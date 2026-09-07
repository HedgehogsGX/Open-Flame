#!/bin/bash -p
# Target Linux/Docker acceptance runner. Default mode is read-only preflight.
# Command output is deliberately suppressed; stdout contains sanitized JSONL only.
set +x
set -eu
set -o pipefail

case $- in
    *p*) ;;
    *) builtin printf '%s\n' 'runner requires a direct shebang launch or /bin/bash -p' >&2; builtin exit 2 ;;
esac

unset BASH_ENV ENV POSIXLY_CORRECT PYTHONHOME PYTHONINSPECT PYTHONOPTIMIZE PYTHONPATH PYTHONSTARTUP
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
export LC_ALL=C
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
COMPOSE_FILE="$REPO_ROOT/deployment/compose.candidate.yaml"
DOCKERFILE="$REPO_ROOT/deployment/Dockerfile.candidate"
BUILD_REQUIREMENTS="$REPO_ROOT/deployment/requirements.build.lock"
RUNTIME_REQUIREMENTS="$REPO_ROOT/deployment/requirements.runtime.lock"
DEFAULT_ENV_FILE="$REPO_ROOT/deployment/.env.candidate.example"
CHECKLIST_FILE="$REPO_ROOT/validation/linux-docker-acceptance.md"
COOKIE_VALIDATOR="$REPO_ROOT/deployment/cookies/validate-cookie-sources.sh"
COOKIE_RUNNER="$REPO_ROOT/deployment/cookies/run-candidate-worker.sh"
PYTHON_BIN=/usr/bin/python3

AUTHORIZATION_TOKEN=I_ACCEPT_TARGET_LINUX_MUTATIONS
MODE=preflight
AUTHORIZATION=
ENV_FILE=$DEFAULT_ENV_FILE
ENV_EXPLICIT=0
COMPOSE_OVERRIDE=
FROZEN_COMPOSE_FILE=
FROZEN_COMPOSE_SNAPSHOT=
TOOL_IMAGE=
BACKUP_ROOT=
RESTORE_PARENT=
RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)-$$"
PROJECT_NAME="vdc-acceptance-$RUN_ID"
BUILD_TAG="vdc-linux-acceptance:$RUN_ID"
BUILT_IMAGE_ID=
RESTORE_NAME="vdc-schema11-acceptance-$RUN_ID"
FAILURES=0
BLOCKED=0
STACK_STARTED=0
LEASE_CONTAINER=
COMPOSE_ARGS=()
BASE_COMPOSE_ARGS=()
RUN_SANDBOX_ARGS=(
    --network none
    --read-only
    --cap-drop ALL
    --security-opt no-new-privileges:true
    --pids-limit 64
    --memory 512m
    --cpus 1.0
)

usage() {
    cat <<'EOF'
Usage:
  deployment/run-linux-acceptance.sh [--preflight] [--env-file ABSOLUTE_FILE]
      [--compose-override ABSOLUTE_FILE]

  deployment/run-linux-acceptance.sh --execute \
      --authorize I_ACCEPT_TARGET_LINUX_MUTATIONS \
      --env-file ABSOLUTE_FILE \
      [--compose-override ABSOLUTE_FILE] \
      --tool-image NAME@sha256:64_LOWERCASE_HEX \
      --backup-root ABSOLUTE_SCHEMA11_BACKUP_DIR \
      --restore-parent ABSOLUTE_EMPTY_PARENT_DIR

Default preflight performs no build, container start, restore, socket creation,
or cleanup. Full execution may access registries, creates a scoped Compose
project/local image/build cache, and writes the dedicated data/socket and restore
roots. It retains host artifacts for reviewed cleanup. Stdout is sanitized JSONL.
EOF
}

emit() {
    check_id=$1
    status=$2
    detail=$3
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s\n' \
        "{\"schema\":\"vdc-linux-docker-acceptance-v1\",\"run_id\":\"$RUN_ID\",\"timestamp\":\"$timestamp\",\"check\":\"$check_id\",\"status\":\"$status\",\"detail\":\"$detail\"}"
}

pass_check() {
    emit "$1" pass "$2"
}

fail_check() {
    FAILURES=$((FAILURES + 1))
    emit "$1" fail "$2"
}

block_check() {
    BLOCKED=$((BLOCKED + 1))
    emit "$1" blocked "$2"
}

fatal_check() {
    fail_check "$1" "$2"
    emit summary fail "failures_${FAILURES}_blocked_${BLOCKED}"
    exit 1
}

canonical_file() {
    candidate=$1
    case "$candidate" in
        /*) ;;
        *) return 1 ;;
    esac
    path_safe_for_mount "$candidate" || return 1
    [ -f "$candidate" ] || return 1
    [ ! -L "$candidate" ] || return 1
    resolved=$(realpath -e -- "$candidate" 2>/dev/null) || return 1
    [ "$resolved" = "$candidate" ] || return 1
    CANONICAL_PATH=$resolved
}

canonical_directory() {
    candidate=$1
    case "$candidate" in
        /*) ;;
        *) return 1 ;;
    esac
    path_safe_for_mount "$candidate" || return 1
    [ -d "$candidate" ] || return 1
    [ ! -L "$candidate" ] || return 1
    resolved=$(realpath -e -- "$candidate" 2>/dev/null) || return 1
    [ "$resolved" = "$candidate" ] || return 1
    [ "$resolved" != / ] || return 1
    CANONICAL_PATH=$resolved
}

path_safe_for_mount() {
    case "$1" in
        *','*|*$'\n'*|*$'\r'*) return 1 ;;
        *) return 0 ;;
    esac
}

directory_is_empty() {
    first_entry=$(find "$1" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null) || return 1
    [ -z "$first_entry" ]
}

host_coredump_policy_safe() {
    [ -r /proc/sys/kernel/core_pattern ] || return 1
    core_pattern=
    IFS= read -r core_pattern < /proc/sys/kernel/core_pattern || return 1
    case "$core_pattern" in
        '|'*) return 1 ;;
        *) return 0 ;;
    esac
}

paths_overlap() {
    first=$1
    second=$2
    if [ "$first" = / ] || [ "$second" = / ]; then
        return 0
    fi
    [ "$first" != "$second" ] || return 0
    case "$first" in
        "$second"/*) return 0 ;;
    esac
    case "$second" in
        "$first"/*) return 0 ;;
    esac
    return 1
}

base_acl_only() {
    candidate=$1
    acl=$(getfacl --absolute-names --numeric --omit-header -- "$candidate" 2>/dev/null) || return 1
    acl_user=0
    acl_group=0
    acl_other=0
    while IFS= read -r acl_line || [ -n "$acl_line" ]; do
        case "$acl_line" in
            "") continue ;;
            user::[r-][w-][x-]) acl_user=$((acl_user + 1)) ;;
            group::[r-][w-][x-]) acl_group=$((acl_group + 1)) ;;
            other::[r-][w-][x-]) acl_other=$((acl_other + 1)) ;;
            *) return 1 ;;
        esac
    done <<EOF
$acl
EOF
    [ "$acl_user" = 1 ] && [ "$acl_group" = 1 ] && [ "$acl_other" = 1 ]
}

root_owned_ancestor_chain() {
    ancestor=$1
    while :; do
        [ -d "$ancestor" ] || return 1
        [ ! -L "$ancestor" ] || return 1
        resolved=$(realpath -e -- "$ancestor" 2>/dev/null) || return 1
        [ "$resolved" = "$ancestor" ] || return 1
        metadata=$(stat -Lc '%F|%u|%a' -- "$ancestor" 2>/dev/null) || return 1
        old_ifs=$IFS
        IFS='|'
        read -r file_type owner_uid permissions <<EOF
$metadata
EOF
        IFS=$old_ifs
        [ "$file_type" = directory ] || return 1
        [ "$owner_uid" = 0 ] || return 1
        case "$permissions" in
            ''|*[!0-7]*) return 1 ;;
        esac
        [ $((8#$permissions & 0022)) -eq 0 ] || return 1
        base_acl_only "$ancestor" || return 1
        [ "$ancestor" = / ] && break
        ancestor=${ancestor%/*}
        [ -n "$ancestor" ] || ancestor=/
    done
}

validate_private_input_metadata() {
    candidate=$1
    canonical_file "$candidate" || return 1
    metadata=$(stat -Lc '%F|%u|%g|%a|%h|%d:%i|%s|%y|%z' -- "$candidate" 2>/dev/null) || return 1
    old_ifs=$IFS
    IFS='|'
    read -r file_type owner_uid owner_gid permissions link_count identity file_size modified changed <<EOF
$metadata
EOF
    IFS=$old_ifs
    [ "$file_type" = "regular file" ] || return 1
    [ "$owner_uid" = 0 ] && [ "$owner_gid" = 0 ] || return 1
    [ "$permissions" = 600 ] || return 1
    [ "$link_count" = 1 ] || return 1
    base_acl_only "$candidate" || return 1
    parent=${candidate%/*}
    [ -n "$parent" ] || parent=/
    root_owned_ancestor_chain "$parent" || return 1
    PRIVATE_INPUT_SNAPSHOT=$identity:$file_size:$modified:$changed
}

validate_policy_input_metadata() {
    candidate=$1
    canonical_file "$candidate" || return 1
    metadata=$(stat -Lc '%F|%u|%g|%a|%h|%d:%i|%s|%y|%z' -- "$candidate" 2>/dev/null) || return 1
    old_ifs=$IFS
    IFS='|'
    read -r file_type owner_uid owner_gid permissions link_count identity file_size modified changed <<EOF
$metadata
EOF
    IFS=$old_ifs
    [ "$file_type" = "regular file" ] || return 1
    [ "$owner_uid" = 0 ] && [ "$owner_gid" = 10001 ] || return 1
    [ "$permissions" = 440 ] || return 1
    [ "$link_count" = 1 ] || return 1
    [ "$file_size" -gt 0 ] 2>/dev/null || return 1
    [ "$file_size" -le 16384 ] 2>/dev/null || return 1
    base_acl_only "$candidate" || return 1
    parent=${candidate%/*}
    [ -n "$parent" ] || parent=/
    root_owned_ancestor_chain "$parent" || return 1
    VALIDATED_POLICY_SNAPSHOT=$identity:$file_size:$modified:$changed
}

private_inputs_unchanged() {
    validate_private_input_metadata "$ENV_FILE" || return 1
    [ "$PRIVATE_INPUT_SNAPSHOT" = "$ENV_FILE_SNAPSHOT" ] || return 1
    if [ -n "$COMPOSE_OVERRIDE" ]; then
        validate_private_input_metadata "$COMPOSE_OVERRIDE" || return 1
        [ "$PRIVATE_INPUT_SNAPSHOT" = "$COMPOSE_OVERRIDE_SNAPSHOT" ] || return 1
    fi
    if [ -n "$FROZEN_COMPOSE_FILE" ]; then
        validate_private_input_metadata "$FROZEN_COMPOSE_FILE" || return 1
        [ "$PRIVATE_INPUT_SNAPSHOT" = "$FROZEN_COMPOSE_SNAPSHOT" ] || return 1
    fi
}

run_cookie_validator() {
    staging_platform=$1
    /usr/bin/env -i \
        "VDC_COOKIE_SOURCE_ROOT=$COOKIE_ROOT_SOURCE" \
        "VDC_COOKIE_MAPPING_FILE=$COOKIE_MAPPING_SOURCE" \
        "VDC_DATA_ROOT=$DATA_SOURCE" \
        "VDC_SOCKET_ROOT=$SOCKET_SOURCE" \
        "VDC_COOKIE_STAGING_PLATFORM=$staging_platform" \
        /bin/sh "$COOKIE_VALIDATOR"
}

build_compose_args() {
    BASE_COMPOSE_ARGS=(
        docker compose
        --project-name "$PROJECT_NAME"
        --env-file "$ENV_FILE"
        -f "$COMPOSE_FILE"
        --profile candidate-real-worker
    )
    COMPOSE_ARGS=(
        docker compose
        --project-name "$PROJECT_NAME"
        --env-file "$ENV_FILE"
        -f "$COMPOSE_FILE"
    )
    if [ -n "$COMPOSE_OVERRIDE" ]; then
        COMPOSE_ARGS+=(-f "$COMPOSE_OVERRIDE")
    fi
    COMPOSE_ARGS+=(--profile candidate-real-worker)
}

validate_config_json() {
    require_digest=$1
    require_cookie=$2
    execution_mode=$3
    "$PYTHON_BIN" -I -c '
import json
import re
import sys

require_digest = sys.argv[1] == "1"
require_cookie = sys.argv[2] == "1"
execution_mode = sys.argv[3] == "1"
expected_cookie_runner = sys.argv[4]
payload = sys.stdin.read()
decoder = json.JSONDecoder()

def decode_document(offset):
    while offset < len(payload) and payload[offset].isspace():
        offset += 1
    return decoder.raw_decode(payload, offset)

baseline, offset = decode_document(0)
document, offset = decode_document(offset)
assert not payload[offset:].strip()
assert set(document) <= {"name", "services", "networks", "configs"}
services = document.get("services", {})
baseline_services = baseline.get("services", {})
required = {"sandbox", "egress-proxy", "relay", "worker"}
assert set(services) == required
assert set(baseline_services) == required
assert not baseline.get("configs")
assert not baseline.get("secrets")
assert not baseline.get("volumes")
assert not document.get("secrets")
assert not document.get("volumes")

networks = document.get("networks", {})
assert set(networks) == {"egress"}
assert networks == baseline.get("networks")
egress_network = networks["egress"]
assert set(egress_network) <= {"name", "driver", "internal"}
assert egress_network.get("driver") == "bridge"
assert not egress_network.get("external")
assert not egress_network.get("internal")
assert not egress_network.get("attachable")
assert not egress_network.get("driver_opts")

tmpfs_sizes = {
    "sandbox": {"8m", "8M", "8388608"},
    "egress-proxy": {"16m", "16M", "16777216"},
    "relay": {"8m", "8M", "8388608"},
    "worker": {"64m", "64M", "67108864"},
}
common_fields = {
    "image",
    "pull_policy",
    "profiles",
    "user",
    "read_only",
    "init",
    "cap_drop",
    "security_opt",
    "restart",
    "command",
    "tmpfs",
    "pids_limit",
    "mem_limit",
    "cpus",
    "healthcheck",
    "stop_grace_period",
}
allowed_fields = {
    "sandbox": common_fields | {"network_mode"},
    "egress-proxy": common_fields | {"volumes", "networks", "ulimits"},
    "relay": common_fields | {"network_mode", "depends_on", "volumes", "ulimits"},
    "worker": common_fields
    | {
        "network_mode",
        "depends_on",
        "environment",
        "entrypoint",
        "volumes",
        "configs",
        "ulimits",
    },
}

for name in required:
    service = services[name]
    assert set(service) <= allowed_fields[name]
    assert not service.get("build")
    assert not service.get("container_name")
    assert service.get("pull_policy") == "never"
    assert service.get("restart") == "unless-stopped"
    assert "candidate-real-worker" in service.get("profiles", [])
    assert str(service.get("user")) == "10001:10001"
    assert service.get("read_only") is True
    assert service.get("init") is True
    assert service.get("cap_drop") == ["ALL"]
    assert not service.get("cap_add")
    assert service.get("security_opt") == ["no-new-privileges:true"]
    assert not service.get("privileged")
    assert not service.get("devices")
    assert not service.get("extra_hosts")
    assert service.get("pids_limit")
    assert service.get("mem_limit")
    assert service.get("cpus")
    tmpfs = service.get("tmpfs", [])
    assert len(tmpfs) == 1
    target, raw_options = str(tmpfs[0]).split(":", 1)
    assert target == "/tmp"
    options = set(raw_options.split(","))
    assert {"rw", "noexec", "nosuid", "nodev", "uid=10001", "gid=10001"} <= options
    mode_options = options & {"mode=0700", "mode=700", "mode=448"}
    assert len(mode_options) == 1
    size_options = {item.split("=", 1)[1] for item in options if item.startswith("size=")}
    assert len(size_options) == 1 and size_options <= tmpfs_sizes[name]
    assert len(options) == 8
    assert service.get("healthcheck")
    assert not service["healthcheck"].get("disable")
    assert not service.get("ports")
    assert not service.get("secrets")
    for forbidden_field in (
        "volumes_from",
        "pid",
        "ipc",
        "uts",
        "userns_mode",
        "group_add",
        "device_cgroup_rules",
        "sysctls",
        "links",
        "external_links",
        "dns",
        "dns_search",
        "dns_opt",
        "runtime",
        "isolation",
        "cgroup",
        "cgroup_parent",
        "credential_spec",
    ):
        assert not service.get(forbidden_field)
    if name != "worker":
        assert not service.get("configs")

assert services["sandbox"].get("network_mode") == "none"
assert services["relay"].get("network_mode") == "service:sandbox"
assert services["worker"].get("network_mode") == "service:sandbox"
assert not services["relay"].get("networks")
assert not services["worker"].get("networks")
assert set(services["egress-proxy"].get("networks", {})) == {"egress"}

for name in ("sandbox", "egress-proxy", "relay"):
    assert not services[name].get("entrypoint")
    assert not services[name].get("environment")

sandbox_command = services["sandbox"].get("command", [])
assert sandbox_command[:3] == ["python", "-I", "-c"] and len(sandbox_command) == 4
expected_sandbox = "import signal,threading; stopped=threading.Event(); signal.signal(signal.SIGTERM,lambda *_:stopped.set()); signal.signal(signal.SIGINT,lambda *_:stopped.set()); stopped.wait()"
assert " ".join(str(sandbox_command[3]).split()) == expected_sandbox

proxy_command = services["egress-proxy"].get("command", [])
assert proxy_command == [
    "video-download-egress-proxy",
    "--unix-socket",
    "/run/vdc-egress/proxy.sock",
    "--allowed-host-file",
    "/etc/vdc/egress-hosts.txt",
    "--max-connections",
    "64",
    "--max-upload-bytes",
    "67108864",
    "--max-download-bytes",
    "536870912",
    "--connect-timeout",
    "10",
    "--idle-timeout",
    "30",
    "--total-timeout",
    "300",
]

relay_command = services["relay"].get("command", [])
assert relay_command == [
    "video-download-unix-relay",
    "--upstream-socket",
    "/run/vdc-egress/proxy.sock",
    "--listen-host",
    "127.0.0.1",
    "--listen-port",
    "18080",
    "--max-connections",
    "16",
    "--max-upload-bytes",
    "67108864",
    "--max-download-bytes",
    "536870912",
    "--connect-timeout",
    "5",
    "--idle-timeout",
    "30",
    "--total-timeout",
    "300",
]

worker = services["worker"]
image = str(worker.get("image", ""))
assert all(str(services[name].get("image", "")) == image for name in required)
core_limit = (worker.get("ulimits") or {}).get("core") or {}
assert int(core_limit.get("soft", -1)) == 0
assert int(core_limit.get("hard", -1)) == 0
if require_digest:
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
environment = worker.get("environment", {})
if isinstance(environment, list):
    environment = dict(item.split("=", 1) for item in environment)
assert set(environment) == {"VDC_ENABLE_CANDIDATE_REAL_WORKER"}
assert str(environment["VDC_ENABLE_CANDIDATE_REAL_WORKER"]) in {"0", "1"}
if execution_mode:
    assert not image.endswith("@sha256:" + ("0" * 64))
    assert str(environment.get("VDC_ENABLE_CANDIDATE_REAL_WORKER")) == "1"

command = worker.get("command", [])
assert isinstance(command, list)
dynamic = object()
expected_worker_command = [
    "video-download-candidate-worker",
    "--data-root",
    "/var/lib/vdc",
    "--database-path",
    "/var/lib/vdc/control.sqlite3",
    "--yt-dlp-executable",
    "/opt/vdc-tools/yt-dlp",
    "--ffmpeg-directory",
    "/opt/vdc-tools",
    "--ffmpeg-executable",
    "/opt/vdc-tools/ffmpeg",
    "--ffprobe-executable",
    "/opt/vdc-tools/ffprobe",
    "--unix-socket-path",
    "/run/vdc-egress/proxy.sock",
    "--yt-dlp-version",
    dynamic,
    "--ffmpeg-version",
    dynamic,
    "--ffprobe-version",
    dynamic,
    "--egress-policy-version",
    dynamic,
    "--relay-port",
    "18080",
    "--max-height",
    "1080",
    "--max-file-bytes",
    "8589934592",
    "--storage-min-free-bytes",
    "1073741824",
    "--socket-timeout-seconds",
    "20",
    "--probe-timeout-seconds",
    "120",
    "--download-timeout-seconds",
    "1800",
    "--ffprobe-timeout-seconds",
    "60",
    "--attempt-timeout-seconds",
    "1800",
    "--max-items-per-source",
    "50",
    "--max-cookie-bytes",
    "16777216",
    "--worker-id",
    "compose-candidate-1",
    "--poll-interval-seconds",
    "2",
]
assert len(command) >= len(expected_worker_command)
assert all(isinstance(value, str) for value in command)
for actual, expected in zip(
    command[:len(expected_worker_command)], expected_worker_command, strict=True
):
    assert expected is dynamic or actual == expected
cookie_tail = command[len(expected_worker_command):]
assert len(cookie_tail) % 2 == 0
assert all(value == "--cookie-source" for value in cookie_tail[::2])
cookie_specs = [str(value) for value in cookie_tail[1::2]]
for flag in (
    "--yt-dlp-version",
    "--ffmpeg-version",
    "--ffprobe-version",
    "--egress-policy-version",
):
    index = expected_worker_command.index(flag)
    value = command[index + 1]
    assert value
    if execution_mode:
        assert not value.startswith("replace-")

assert len(cookie_specs) <= 6
cookie_platforms = set()
cookie_references = set()
cookie_targets = set()
for spec in cookie_specs:
    identity, target = spec.split("=", 1)
    platform, opaque_ref = identity.split(":", 1)
    assert platform in {"x", "youtube", "bilibili", "douyin", "tiktok", "instagram"}
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", opaque_ref)
    assert platform not in cookie_platforms
    assert opaque_ref not in cookie_references
    assert target.startswith("/")
    assert target not in cookie_targets
    assert target != "/var/lib/vdc" and not target.startswith("/var/lib/vdc/")
    assert target != "/tmp" and not target.startswith("/tmp/")
    assert target != "/run/vdc-egress" and not target.startswith("/run/vdc-egress/")
    cookie_platforms.add(platform)
    cookie_references.add(opaque_ref)
    cookie_targets.add(target)

worker_volumes = worker.get("volumes", [])
other_volume_sources = {
    item.get("source")
    for name, service in services.items()
    if name != "worker"
    for item in service.get("volumes", [])
}

def mount_paths_overlap(first, second):
    assert isinstance(first, str) and first.startswith("/")
    assert isinstance(second, str) and second.startswith("/")
    return (
        first == "/"
        or second == "/"
        or first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )

cookie_mount_sources = set()
for target in cookie_targets:
    matching = [item for item in worker_volumes if item.get("target") == target]
    assert len(matching) == 1
    assert matching[0].get("type") == "bind"
    assert matching[0].get("read_only") is True
    assert matching[0].get("source") not in other_volume_sources
    assert matching[0].get("source") not in cookie_mount_sources
    cookie_mount_sources.add(matching[0].get("source"))

entrypoint = worker.get("entrypoint", [])
wrapper_mode = entrypoint == ["/bin/sh", "/run/vdc-cookie-runner.sh"]
assert not (wrapper_mode and cookie_specs)
if wrapper_mode:
    expected_targets = {
        "/run/vdc-cookie-sources",
        "/run/vdc-cookie-mapping.env",
    }
    wrapper_mounts = [
        item for item in worker_volumes if item.get("target") in expected_targets
    ]
    assert {item.get("target") for item in wrapper_mounts} == expected_targets
    assert all(item.get("type") == "bind" for item in wrapper_mounts)
    assert all(item.get("read_only") is True for item in wrapper_mounts)
    assert all(
        not any(
            mount_paths_overlap(item.get("source"), other_source)
            for other_source in other_volume_sources
        )
        for item in wrapper_mounts
    )
    configs = worker.get("configs", [])
    assert len(configs) == 1
    assert configs[0].get("target") == "/run/vdc-cookie-runner.sh"
    assert configs[0].get("source") == "cookie-runner"
    assert configs[0].get("mode") in {292, "0444"}
    assert set(configs[0]) <= {"source", "target", "mode"}
    top_level_configs = document.get("configs", {})
    assert set(top_level_configs) == {"cookie-runner"}
    runner_config = top_level_configs["cookie-runner"]
    assert set(runner_config) == {"file"}
    assert runner_config.get("file") == expected_cookie_runner

    assert set(document) == set(baseline) | {"configs"}
    for key in set(baseline) - {"services"}:
        assert document[key] == baseline[key]
    for name in required - {"worker"}:
        assert services[name] == baseline_services[name]
    baseline_worker = baseline_services["worker"]
    assert set(worker) == set(baseline_worker) | {"entrypoint", "configs"}
    for key in set(baseline_worker) - {"volumes"}:
        assert worker[key] == baseline_worker[key]
    baseline_volumes = {
        item.get("target"): item for item in baseline_worker.get("volumes", [])
    }
    effective_volumes = {
        item.get("target"): item for item in worker.get("volumes", [])
    }
    assert len(baseline_volumes) == len(baseline_worker.get("volumes", []))
    for target, item in baseline_volumes.items():
        assert effective_volumes.get(target) == item
else:
    assert not entrypoint
    assert not worker.get("configs")
    assert not document.get("configs")
    assert document == baseline

def bind_volumes(name):
    volumes = services[name].get("volumes", [])
    assert all(
        set(item) == {"type", "source", "target", "read_only", "bind"}
        for item in volumes
    )
    assert all(item.get("type") == "bind" for item in volumes)
    assert all(str(item.get("source", "")).startswith("/") for item in volumes)
    assert all(item.get("bind") == {"create_host_path": False} for item in volumes)
    by_target = {item.get("target"): item for item in volumes}
    assert len(by_target) == len(volumes)
    return by_target

assert bind_volumes("sandbox") == {}
proxy_volumes = bind_volumes("egress-proxy")
relay_volumes = bind_volumes("relay")
worker_volumes_by_target = bind_volumes("worker")
assert set(proxy_volumes) == {"/run/vdc-egress", "/etc/vdc/egress-hosts.txt"}
assert proxy_volumes["/run/vdc-egress"].get("read_only") is not True
assert proxy_volumes["/etc/vdc/egress-hosts.txt"].get("read_only") is True
assert set(relay_volumes) == {"/run/vdc-egress"}
assert relay_volumes["/run/vdc-egress"].get("read_only") is True
worker_required_targets = {"/var/lib/vdc", "/run/vdc-egress"}
worker_cookie_targets = cookie_targets
if wrapper_mode:
    worker_cookie_targets = {"/run/vdc-cookie-sources", "/run/vdc-cookie-mapping.env"}
assert set(worker_volumes_by_target) == worker_required_targets | worker_cookie_targets
assert worker_volumes_by_target["/var/lib/vdc"].get("read_only") is not True
assert worker_volumes_by_target["/run/vdc-egress"].get("read_only") is True
assert proxy_volumes["/run/vdc-egress"].get("source") == relay_volumes["/run/vdc-egress"].get("source")
assert proxy_volumes["/run/vdc-egress"].get("source") == worker_volumes_by_target["/run/vdc-egress"].get("source")
assert worker_volumes_by_target["/var/lib/vdc"].get("source") not in {
    item.get("source") for item in proxy_volumes.values()
}

if require_cookie:
    # Full acceptance is deliberately narrower than the product contract: the
    # reviewed wrapper is the only shape with equivalent host-path validation.
    assert wrapper_mode
' "$require_digest" "$require_cookie" "$execution_mode" "$COOKIE_RUNNER"
}

run_config_check() {
    require_digest=$1
    require_cookie=$2
    execution_mode=$3
    image_override=${4:-}
    if [ -n "$image_override" ]; then
        base_json=$(VDC_CANDIDATE_IMAGE="$image_override" "${BASE_COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
        effective_json=$(VDC_CANDIDATE_IMAGE="$image_override" "${COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
    else
        base_json=$("${BASE_COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
        effective_json=$("${COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
    fi
    printf '%s\n%s\n' "$base_json" "$effective_json" \
        | validate_config_json "$require_digest" "$require_cookie" "$execution_mode" \
            >/dev/null 2>&1
}

freeze_effective_config() {
    image_override=$1
    base_json=$(VDC_CANDIDATE_IMAGE="$image_override" "${BASE_COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
    effective_json=$(VDC_CANDIDATE_IMAGE="$image_override" "${COMPOSE_ARGS[@]}" config --format json 2>/dev/null) || return 1
    printf '%s\n%s\n' "$base_json" "$effective_json" \
        | validate_config_json 0 1 1 >/dev/null 2>&1 || return 1
    printf '%s\n' "$effective_json" \
        | "$PYTHON_BIN" -I -c '
import json
import sys

def reject_dollar(value):
    if isinstance(value, str):
        assert "$" not in value
    elif isinstance(value, list):
        for item in value:
            reject_dollar(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_dollar(key)
            reject_dollar(item)

reject_dollar(json.load(sys.stdin))
' >/dev/null 2>&1 || return 1

    frozen_parent=${ENV_FILE%/*}
    [ -n "$frozen_parent" ] || frozen_parent=/
    FROZEN_COMPOSE_FILE="$frozen_parent/.vdc-effective-$RUN_ID.json"
    [ ! -e "$FROZEN_COMPOSE_FILE" ] && [ ! -L "$FROZEN_COMPOSE_FILE" ] || return 1
    if ! (umask 077; set -C; printf '%s\n' "$effective_json" > "$FROZEN_COMPOSE_FILE") 2>/dev/null; then
        return 1
    fi
    validate_private_input_metadata "$FROZEN_COMPOSE_FILE" || return 1
    FROZEN_COMPOSE_SNAPSHOT=$PRIVATE_INPUT_SNAPSHOT
    frozen_args=(
        docker compose
        --project-name "$PROJECT_NAME"
        -f "$FROZEN_COMPOSE_FILE"
        --profile candidate-real-worker
    )
    frozen_json=$("${frozen_args[@]}" config --format json 2>/dev/null) || return 1
    printf '%s\n%s\n' "$effective_json" "$frozen_json" \
        | "$PYTHON_BIN" -I -c '
import json
import sys

payload = sys.stdin.read()
decoder = json.JSONDecoder()

def decode_document(offset):
    while offset < len(payload) and payload[offset].isspace():
        offset += 1
    return decoder.raw_decode(payload, offset)

expected, offset = decode_document(0)
actual, offset = decode_document(offset)
assert not payload[offset:].strip()
assert actual == expected
' >/dev/null 2>&1 || return 1
    printf '%s\n%s\n' "$base_json" "$frozen_json" \
        | validate_config_json 0 1 1 >/dev/null 2>&1 || return 1
    validate_private_input_metadata "$FROZEN_COMPOSE_FILE" || return 1
    [ "$PRIVATE_INPUT_SNAPSHOT" = "$FROZEN_COMPOSE_SNAPSHOT" ] || return 1
    COMPOSE_ARGS=("${frozen_args[@]}")
}

extract_bind_source() {
    service_name=$1
    target_path=$2
    "${COMPOSE_ARGS[@]}" config --format json 2>/dev/null \
        | "$PYTHON_BIN" -I -c '
import json
import sys

service_name, target_path = sys.argv[1:]
document = json.load(sys.stdin)
volumes = document["services"][service_name].get("volumes", [])
matching = [item for item in volumes if item.get("target") == target_path]
assert len(matching) == 1
item = matching[0]
assert item.get("type") == "bind"
source = item.get("source")
assert isinstance(source, str) and source.startswith("/")
print(source)
' "$service_name" "$target_path"
}

compose_local() {
    VDC_CANDIDATE_IMAGE="$BUILD_TAG" "${COMPOSE_ARGS[@]}" "$@"
}

container_id_for() {
    compose_local ps -q "$1" 2>/dev/null
}

wait_healthy() {
    service_name=$1
    deadline=$((SECONDS + 150))
    while [ "$SECONDS" -lt "$deadline" ]; do
        container_id=$(container_id_for "$service_name") || return 1
        case "$container_id" in
            ''|*[!0-9a-f]*) sleep 2; continue ;;
        esac
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null) || return 1
        case "$health" in
            healthy) return 0 ;;
            unhealthy) return 1 ;;
            *) sleep 2 ;;
        esac
    done
    return 1
}

validate_runtime_container() {
    expected_image_id=$1
    "$PYTHON_BIN" -I -c '
import json
import sys

payload = json.load(sys.stdin)
assert len(payload) == 1
container = payload[0]
assert container["Image"] == sys.argv[1]
config = container["Config"]
host = container["HostConfig"]
network = container["NetworkSettings"]
assert config.get("User") == "10001:10001"
assert host.get("ReadonlyRootfs") is True
assert host.get("Init") is True
assert "ALL" in (host.get("CapDrop") or [])
assert "no-new-privileges:true" in (host.get("SecurityOpt") or [])
assert int(host.get("PidsLimit") or 0) > 0
assert int(host.get("Memory") or 0) > 0
assert int(host.get("NanoCpus") or 0) > 0
assert not any((network.get("Ports") or {}).values())
' "$expected_image_id"
}

cleanup_on_exit() {
    if [ -n "$LEASE_CONTAINER" ]; then
        if docker inspect "$LEASE_CONTAINER" >/dev/null 2>&1 \
            && ! docker stop --time 5 "$LEASE_CONTAINER" >/dev/null 2>&1; then
            emit cleanup fail fixture_container_stop_failed
        fi
    fi
    if [ "$STACK_STARTED" -eq 1 ] && [ "${#COMPOSE_ARGS[@]}" -gt 0 ]; then
        if ! compose_local stop -t 30 >/dev/null 2>&1; then
            emit cleanup fail scoped_stack_stop_failed
        fi
    fi
    if [ -n "$FROZEN_COMPOSE_FILE" ] && [ -e "$FROZEN_COMPOSE_FILE" ]; then
        if validate_private_input_metadata "$FROZEN_COMPOSE_FILE" \
            && { [ -z "$FROZEN_COMPOSE_SNAPSHOT" ] \
                || [ "$PRIVATE_INPUT_SNAPSHOT" = "$FROZEN_COMPOSE_SNAPSHOT" ]; }; then
            if ! rm -f -- "$FROZEN_COMPOSE_FILE"; then
                emit cleanup fail frozen_compose_remove_failed
            fi
        else
            emit cleanup fail frozen_compose_identity_changed
        fi
    fi
}

trap cleanup_on_exit EXIT

while [ "$#" -gt 0 ]; do
    case "$1" in
        --preflight)
            MODE=preflight
            shift
            ;;
        --execute)
            MODE=execute
            shift
            ;;
        --authorize)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            AUTHORIZATION=$2
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            ENV_FILE=$2
            ENV_EXPLICIT=1
            shift 2
            ;;
        --compose-override)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            COMPOSE_OVERRIDE=$2
            shift 2
            ;;
        --tool-image)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            TOOL_IMAGE=$2
            shift 2
            ;;
        --backup-root)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            BACKUP_ROOT=$2
            shift 2
            ;;
        --restore-parent)
            [ "$#" -ge 2 ] || fatal_check cli_arguments missing_option_value
            RESTORE_PARENT=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fatal_check cli_arguments unsupported_option
            ;;
    esac
done

emit meta info "mode_$MODE"

assets_ok=1
for required_asset in "$COMPOSE_FILE" "$DOCKERFILE" "$BUILD_REQUIREMENTS" "$RUNTIME_REQUIREMENTS" "$DEFAULT_ENV_FILE" "$CHECKLIST_FILE" "$COOKIE_VALIDATOR" "$COOKIE_RUNNER"; do
    if [ ! -f "$required_asset" ] || [ -L "$required_asset" ]; then
        assets_ok=0
    fi
done
if [ "$assets_ok" -eq 1 ]; then
    pass_check fixed_assets repository_assets_present
else
    fatal_check fixed_assets repository_asset_missing
fi

if [ "$(uname -s 2>/dev/null || printf unknown)" = Linux ]; then
    pass_check host_linux linux_kernel_detected
    HOST_LINUX=1
else
    block_check host_linux target_linux_required
    HOST_LINUX=0
fi

if [ "$HOST_LINUX" -eq 1 ] && host_coredump_policy_safe; then
    pass_check host_coredump_policy non_pipe_core_pattern_with_worker_rlimit_zero
    HOST_COREDUMP_SAFE=1
else
    block_check host_coredump_policy pipe_collector_or_policy_unavailable
    HOST_COREDUMP_SAFE=0
fi

if command -v realpath >/dev/null 2>&1 \
    && command -v stat >/dev/null 2>&1 \
    && command -v find >/dev/null 2>&1 \
    && command -v getfacl >/dev/null 2>&1 \
    && [ -x /usr/bin/env ] \
    && [ -x /bin/sh ] \
    && [ -x "$PYTHON_BIN" ] \
    && command -v rm >/dev/null 2>&1 \
    && "$PYTHON_BIN" -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
    pass_check host_tools metadata_tools_present
    HOST_TOOLS=1
else
    block_check host_tools metadata_tools_required
    HOST_TOOLS=0
fi

if [ "$HOST_TOOLS" -eq 1 ] && canonical_file "$ENV_FILE"; then
    ENV_FILE=$CANONICAL_PATH
    if [ -n "$COMPOSE_OVERRIDE" ]; then
        if canonical_file "$COMPOSE_OVERRIDE"; then
            COMPOSE_OVERRIDE=$CANONICAL_PATH
        else
            fatal_check compose_override invalid_private_override
        fi
    fi
    pass_check config_inputs canonical_files_only
    CONFIG_INPUTS=1
else
    block_check config_inputs canonical_env_file_required
    CONFIG_INPUTS=0
fi

if command -v docker >/dev/null 2>&1 \
    && docker compose version >/dev/null 2>&1 \
    && context_name=$(docker context show 2>/dev/null) \
    && context_endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$context_name" 2>/dev/null); then
    if [ -n "${DOCKER_CONTEXT:-}" ]; then
        effective_endpoint=$context_endpoint
    elif [ -n "${DOCKER_HOST:-}" ]; then
        effective_endpoint=$DOCKER_HOST
    else
        effective_endpoint=$context_endpoint
    fi
    case "$effective_endpoint" in
        unix:///*) local_endpoint=1 ;;
        *) local_endpoint=0 ;;
    esac
    if [ "$local_endpoint" -eq 1 ] \
        && docker info >/dev/null 2>&1 \
        && daemon_os=$(docker info --format '{{.OSType}}' 2>/dev/null) \
        && [ "$daemon_os" = linux ]; then
        pass_check docker_daemon local_linux_docker_compose_v2_ready
        DOCKER_READY=1
    else
        block_check docker_daemon local_linux_docker_daemon_required
        DOCKER_READY=0
    fi
else
    block_check docker_daemon target_docker_daemon_required
    DOCKER_READY=0
fi

if [ "$CONFIG_INPUTS" -eq 1 ] && [ "$DOCKER_READY" -eq 1 ]; then
    build_compose_args
    if run_config_check 1 0 0; then
        pass_check compose_config effective_config_safe
    else
        fail_check compose_config effective_config_rejected
    fi
else
    block_check compose_config prerequisites_unavailable
fi

if [ "$MODE" = preflight ]; then
    if [ "$FAILURES" -gt 0 ]; then
        emit summary fail "failures_${FAILURES}_blocked_${BLOCKED}"
        exit 1
    fi
    if [ "$BLOCKED" -gt 0 ]; then
        emit summary blocked "failures_${FAILURES}_blocked_${BLOCKED}"
        exit 2
    fi
    emit summary pass failures_0_blocked_0
    exit 0
fi

if [ "$MODE" != execute ]; then
    fatal_check execution_mode invalid_mode
fi
if [ "$AUTHORIZATION" != "$AUTHORIZATION_TOKEN" ]; then
    fatal_check authorization explicit_authorization_required
fi
pass_check authorization target_mutation_authorized
if [ "$EUID" -ne 0 ]; then
    fatal_check authorization effective_root_required
fi
pass_check root_execution effective_root_confirmed
for docker_control_name in \
    DOCKER_CONTEXT \
    DOCKER_HOST \
    DOCKER_CONFIG \
    DOCKER_CERT_PATH \
    DOCKER_TLS_VERIFY \
    BUILDKIT_HOST \
    BUILDX_BUILDER \
    COMPOSE_FILE \
    COMPOSE_PROJECT_NAME \
    COMPOSE_PROFILES; do
    if [[ -v "$docker_control_name" ]]; then
        fatal_check docker_daemon inherited_docker_or_compose_control_rejected
    fi
done
if [ "${VDC_ENABLE_X_GRAPH_V2:-0}" != 0 ]; then
    fatal_check graph_gate x_graph_v2_must_remain_disabled
fi
pass_check graph_gate x_graph_v2_disabled_for_acceptance
for interpolation_name in \
    VDC_CANDIDATE_IMAGE \
    VDC_ENABLE_CANDIDATE_REAL_WORKER \
    VDC_DATA_ROOT \
    VDC_SOCKET_ROOT \
    VDC_EGRESS_POLICY_FILE \
    VDC_COOKIE_SOURCE_ROOT \
    VDC_COOKIE_MAPPING_FILE \
    VDC_YT_DLP_VERSION \
    VDC_FFMPEG_VERSION \
    VDC_FFPROBE_VERSION \
    VDC_EGRESS_POLICY_VERSION; do
    if [[ -v "$interpolation_name" ]]; then
        fatal_check private_env inherited_compose_variable_rejected
    fi
done
if [ "$ENV_EXPLICIT" -ne 1 ] || [ "$ENV_FILE" = "$DEFAULT_ENV_FILE" ]; then
    fatal_check authorization private_env_file_required
fi
if [ "$HOST_LINUX" -ne 1 ] || [ "$HOST_TOOLS" -ne 1 ] \
    || [ "$HOST_COREDUMP_SAFE" -ne 1 ] || [ "$DOCKER_READY" -ne 1 ]; then
    fatal_check execution_prerequisites target_linux_docker_required
fi
if [ "$FAILURES" -gt 0 ] || [ "$BLOCKED" -gt 0 ]; then
    fatal_check execution_prerequisites preflight_not_clean
fi

if paths_overlap "$ENV_FILE" "$REPO_ROOT"; then
    fatal_check private_env repository_local_private_env_rejected
fi
if [ -n "$COMPOSE_OVERRIDE" ] && paths_overlap "$COMPOSE_OVERRIDE" "$REPO_ROOT"; then
    fatal_check compose_override repository_local_private_override_rejected
fi
if ! validate_private_input_metadata "$ENV_FILE"; then
    fatal_check private_env root_owned_private_input_required
fi
ENV_FILE_SNAPSHOT=$PRIVATE_INPUT_SNAPSHOT
if ! "$PYTHON_BIN" -I - "$ENV_FILE" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import re
import sys

expected = {
    "VDC_CANDIDATE_IMAGE",
    "VDC_ENABLE_CANDIDATE_REAL_WORKER",
    "VDC_DATA_ROOT",
    "VDC_SOCKET_ROOT",
    "VDC_EGRESS_POLICY_FILE",
    "VDC_COOKIE_SOURCE_ROOT",
    "VDC_COOKIE_MAPPING_FILE",
    "VDC_YT_DLP_VERSION",
    "VDC_FFMPEG_VERSION",
    "VDC_FFPROBE_VERSION",
    "VDC_EGRESS_POLICY_VERSION",
}
path = Path(sys.argv[1])
assert path.stat().st_size <= 16 * 1024
keys = []
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key, _ = line.split("=", 1)
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
    assert key not in keys
    keys.append(key)
assert set(keys) == expected
PY
then
    fatal_check private_env unexpected_or_missing_environment_key
fi
pass_check private_env root_owned_stable_permissions_acl_and_keys_restricted
if [ -n "$COMPOSE_OVERRIDE" ]; then
    if ! validate_private_input_metadata "$COMPOSE_OVERRIDE"; then
        fatal_check compose_override root_owned_private_input_required
    fi
    COMPOSE_OVERRIDE_SNAPSHOT=$PRIVATE_INPUT_SNAPSHOT
    pass_check compose_override root_owned_stable_permissions_and_acl_restricted
fi

if [[ ! "$TOOL_IMAGE" =~ ^[^[:space:]]+@sha256:[0-9a-f]{64}$ ]]; then
    fatal_check tool_image immutable_tool_image_required
fi
tool_digest=${TOOL_IMAGE##*@sha256:}
if [ "$tool_digest" = "$(printf '%064d' 0)" ]; then
    fatal_check tool_image placeholder_tool_image_rejected
fi
pass_check tool_image immutable_tool_image_selected

if ! canonical_directory "$BACKUP_ROOT"; then
    fatal_check schema11_restore canonical_backup_root_required
fi
BACKUP_ROOT=$CANONICAL_PATH
if ! canonical_directory "$RESTORE_PARENT"; then
    fatal_check schema11_restore canonical_restore_parent_required
fi
RESTORE_PARENT=$CANONICAL_PATH
if paths_overlap "$BACKUP_ROOT" "$REPO_ROOT" \
    || paths_overlap "$RESTORE_PARENT" "$REPO_ROOT"; then
    fatal_check schema11_restore repository_local_recovery_path_rejected
fi
if ! directory_is_empty "$RESTORE_PARENT"; then
    fatal_check schema11_restore restore_parent_must_be_empty
fi
restore_metadata=$(stat -Lc '%u:%g:%a' -- "$RESTORE_PARENT" 2>/dev/null) || fatal_check schema11_restore restore_parent_metadata_unavailable
if [ "$restore_metadata" != 10001:10001:750 ]; then
    fatal_check schema11_restore restore_parent_ownership_or_mode_mismatch
fi
case "$RESTORE_PARENT/" in
    "$BACKUP_ROOT/"*|"$BACKUP_ROOT"/) fatal_check schema11_restore overlapping_restore_paths ;;
esac
case "$BACKUP_ROOT/" in
    "$RESTORE_PARENT/"*) fatal_check schema11_restore overlapping_restore_paths ;;
esac
RESTORE_TARGET="$RESTORE_PARENT/$RESTORE_NAME"
if [ -e "$RESTORE_TARGET" ] || [ -L "$RESTORE_TARGET" ]; then
    fatal_check schema11_restore restore_target_must_not_exist
fi

if ! private_inputs_unchanged; then
    fatal_check private_env private_input_changed_after_validation
fi
if ! run_config_check 1 1 1; then
    fatal_check cookie_compose optional_worker_only_cookie_contract_required
fi
pass_check cookie_compose effective_cookie_contract_present

if ! DATA_SOURCE=$(extract_bind_source worker /var/lib/vdc 2>/dev/null); then
    fatal_check dedicated_roots data_bind_unavailable
fi
if ! canonical_directory "$DATA_SOURCE"; then
    fatal_check dedicated_roots invalid_data_root
fi
DATA_SOURCE=$CANONICAL_PATH
if ! SOCKET_SOURCE=$(extract_bind_source egress-proxy /run/vdc-egress 2>/dev/null); then
    fatal_check dedicated_roots socket_bind_unavailable
fi
if ! canonical_directory "$SOCKET_SOURCE"; then
    fatal_check dedicated_roots invalid_socket_root
fi
SOCKET_SOURCE=$CANONICAL_PATH
if ! POLICY_SOURCE=$(extract_bind_source egress-proxy /etc/vdc/egress-hosts.txt 2>/dev/null); then
    fatal_check deny_only_policy policy_bind_unavailable
fi
if ! canonical_file "$POLICY_SOURCE"; then
    fatal_check deny_only_policy invalid_policy_file
fi
POLICY_SOURCE=$CANONICAL_PATH
if ! validate_policy_input_metadata "$POLICY_SOURCE"; then
    fatal_check deny_only_policy root_owned_readonly_policy_required
fi
POLICY_SOURCE_SNAPSHOT=$VALIDATED_POLICY_SNAPSHOT

for protected_root in "$REPO_ROOT" "$DATA_SOURCE" "$SOCKET_SOURCE" "$BACKUP_ROOT" "$RESTORE_PARENT" "$ENV_FILE"; do
    if paths_overlap "$POLICY_SOURCE" "$protected_root"; then
        fatal_check deny_only_policy policy_overlaps_protected_path
    fi
done
if [ -n "$COMPOSE_OVERRIDE" ] && paths_overlap "$POLICY_SOURCE" "$COMPOSE_OVERRIDE"; then
    fatal_check deny_only_policy policy_overlaps_private_override
fi

if paths_overlap "$DATA_SOURCE" "$SOCKET_SOURCE"; then
    fatal_check dedicated_roots data_and_socket_roots_overlap
fi
for runtime_root in "$DATA_SOURCE" "$SOCKET_SOURCE"; do
    if paths_overlap "$runtime_root" "$REPO_ROOT" \
        || paths_overlap "$runtime_root" "$BACKUP_ROOT" \
        || paths_overlap "$runtime_root" "$RESTORE_PARENT" \
        || paths_overlap "$runtime_root" "$POLICY_SOURCE" \
        || paths_overlap "$runtime_root" "$ENV_FILE"; then
        fatal_check dedicated_roots runtime_root_overlaps_protected_path
    fi
    if [ -n "$COMPOSE_OVERRIDE" ] && paths_overlap "$runtime_root" "$COMPOSE_OVERRIDE"; then
        fatal_check dedicated_roots runtime_root_overlaps_private_override
    fi
done

case "$RESTORE_PARENT/" in
    "$DATA_SOURCE/"*|"$DATA_SOURCE"/|"$SOCKET_SOURCE/"*|"$SOCKET_SOURCE"/) fatal_check schema11_restore restore_parent_overlaps_runtime_root ;;
esac
case "$DATA_SOURCE/" in
    "$RESTORE_PARENT/"*) fatal_check schema11_restore data_root_overlaps_restore_parent ;;
esac
case "$SOCKET_SOURCE/" in
    "$RESTORE_PARENT/"*) fatal_check schema11_restore socket_root_overlaps_restore_parent ;;
esac

if COOKIE_ROOT_SOURCE=$(extract_bind_source worker /run/vdc-cookie-sources 2>/dev/null); then
    if ! COOKIE_MAPPING_SOURCE=$(extract_bind_source worker /run/vdc-cookie-mapping.env 2>/dev/null); then
        fatal_check cookie_host_metadata wrapper_mapping_bind_unavailable
    fi
    if ! canonical_directory "$COOKIE_ROOT_SOURCE"; then
        fatal_check cookie_host_metadata invalid_cookie_source_root
    fi
    COOKIE_ROOT_SOURCE=$CANONICAL_PATH
    if ! canonical_file "$COOKIE_MAPPING_SOURCE"; then
        fatal_check cookie_host_metadata invalid_cookie_mapping_file
    fi
    COOKIE_MAPPING_SOURCE=$CANONICAL_PATH
    for protected_root in "$REPO_ROOT" "$DATA_SOURCE" "$SOCKET_SOURCE" "$POLICY_SOURCE" "$BACKUP_ROOT" "$RESTORE_PARENT"; do
        if paths_overlap "$COOKIE_ROOT_SOURCE" "$protected_root" \
            || paths_overlap "$COOKIE_MAPPING_SOURCE" "$protected_root"; then
            fatal_check cookie_host_metadata cookie_path_overlaps_protected_root
        fi
    done
    if ! run_cookie_validator "" >/dev/null 2>&1; then
        fatal_check cookie_host_metadata deployment_metadata_validation_failed
    fi
    if ! "$PYTHON_BIN" -I - "$COOKIE_MAPPING_SOURCE" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import re
import sys

expected = {
    "VDC_COOKIE_X_OPAQUE_REF",
    "VDC_COOKIE_YOUTUBE_OPAQUE_REF",
    "VDC_COOKIE_BILIBILI_OPAQUE_REF",
    "VDC_COOKIE_DOUYIN_OPAQUE_REF",
    "VDC_COOKIE_TIKTOK_OPAQUE_REF",
    "VDC_COOKIE_INSTAGRAM_OPAQUE_REF",
}
entries = {}
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key, value = line.split("=", 1)
    assert key not in entries
    entries[key] = value
assert set(entries) == expected
values = list(entries.values())
assert len(set(values)) == 6
assert all(re.fullmatch(r"acceptance-[A-Za-z0-9_.-]{1,53}", value) for value in values)
PY
    then
        fatal_check cookie_host_metadata six_synthetic_refs_required_for_acceptance
    fi
    pass_check cookie_host_metadata optional_wrapper_and_synthetic_ceiling_validated
else
    fatal_check cookie_host_metadata reviewed_wrapper_required
fi

execution_inputs_unchanged() {
    private_inputs_unchanged || return 1
    host_coredump_policy_safe || return 1
    validate_policy_input_metadata "$POLICY_SOURCE" || return 1
    [ "$VALIDATED_POLICY_SNAPSHOT" = "$POLICY_SOURCE_SNAPSHOT" ] || return 1
    if [ -n "$BUILT_IMAGE_ID" ]; then
        observed_image_id=$(docker image inspect --format '{{.Id}}' "$BUILT_IMAGE_ID" 2>/dev/null) || return 1
        [ "$observed_image_id" = "$BUILT_IMAGE_ID" ] || return 1
    fi
    run_cookie_validator "" >/dev/null 2>&1
}

if ! directory_is_empty "$DATA_SOURCE" || ! directory_is_empty "$SOCKET_SOURCE"; then
    fatal_check dedicated_roots nonempty_acceptance_root_rejected
fi
data_metadata=$(stat -Lc '%u:%g:%a' -- "$DATA_SOURCE" 2>/dev/null) || fatal_check dedicated_roots data_metadata_unavailable
socket_metadata=$(stat -Lc '%u:%g:%a' -- "$SOCKET_SOURCE" 2>/dev/null) || fatal_check dedicated_roots socket_metadata_unavailable
if [ "$data_metadata" != 10001:10001:750 ] || [ "$socket_metadata" != 10001:10001:770 ]; then
    fatal_check dedicated_roots ownership_or_mode_mismatch
fi
pass_check dedicated_roots empty_scoped_roots_ready

if "$PYTHON_BIN" -I - "$POLICY_SOURCE" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import sys

lines = [
    line.strip()
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
assert lines == ["replace.invalid"]
PY
then
    pass_check deny_only_policy no_real_egress_hosts
else
    fatal_check deny_only_policy acceptance_policy_must_be_replace_invalid_only
fi

if docker image inspect "$BUILD_TAG" >/dev/null 2>&1; then
    fatal_check scoped_project acceptance_image_tag_already_exists
fi
existing_project_ids=$(compose_local ps -a -q 2>/dev/null) || fatal_check scoped_project project_inspection_failed
if [ -n "$existing_project_ids" ]; then
    fatal_check scoped_project acceptance_project_already_exists
fi
pass_check scoped_project fresh_project_and_image_names

if docker build \
    --file "$DOCKERFILE" \
    --build-arg "VDC_TOOL_IMAGE=$TOOL_IMAGE" \
    --tag "$BUILD_TAG" \
    "$REPO_ROOT" >/dev/null 2>&1; then
    pass_check image_build candidate_image_built
else
    fatal_check image_build candidate_image_build_failed
fi

BUILT_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$BUILD_TAG" 2>/dev/null) \
    || fatal_check image_identity built_image_id_unavailable
if [[ ! "$BUILT_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fatal_check image_identity invalid_built_image_id
fi
pass_check image_identity immutable_local_image_id_captured

if docker image inspect "$BUILT_IMAGE_ID" 2>/dev/null \
    | "$PYTHON_BIN" -I -c '
import json
import sys

payload = json.load(sys.stdin)
assert len(payload) == 1
config = payload[0]["Config"]
assert config.get("User") == "10001:10001"
labels = config.get("Labels") or {}
assert "candidate" in labels.get("org.opencontainers.image.description", "").lower()
' >/dev/null 2>&1; then
    pass_check image_contract nonroot_candidate_image
else
    fatal_check image_contract built_image_contract_failed
fi

if docker run --rm \
    "${RUN_SANDBOX_ARGS[@]}" \
    --user 10001:10001 \
    "$BUILT_IMAGE_ID" \
    python -I -c '
from importlib.metadata import PackageNotFoundError, version

from video_download_control.adapters.yt_dlp import YtDlpAdapter
from video_download_control.database import SCHEMA_VERSION

expected = {
    "annotated-doc": "0.0.5",
    "annotated-types": "0.8.0",
    "anyio": "4.14.2",
    "click": "8.5.0",
    "fastapi": "0.141.1",
    "h11": "0.16.0",
    "idna": "3.19",
    "pillow": "12.3.0",
    "pydantic": "2.13.5",
    "pydantic-core": "2.46.5",
    "starlette": "1.6.0",
    "typing-extensions": "4.16.0",
    "typing-inspection": "0.4.4",
    "uvicorn": "0.52.4",
    "video-download-control": "0.26.0",
}
assert all(version(name) == expected_version for name, expected_version in expected.items())
for build_only in ("hatchling", "packaging", "pathspec", "pluggy", "trove-classifiers"):
    try:
        version(build_only)
    except PackageNotFoundError:
        continue
    raise AssertionError("build-only dependency leaked into runtime")
raise SystemExit(0 if SCHEMA_VERSION == 11 and YtDlpAdapter.supports_exact_selector is False else 1)
' >/dev/null 2>&1; then
    pass_check image_runtime_contract schema11_exact_runtime_lock_and_no_real_exact_selector
else
    fatal_check image_runtime_contract built_runtime_contract_failed
fi

if ! execution_inputs_unchanged; then
    fatal_check private_env private_input_changed_before_start
fi
if freeze_effective_config "$BUILT_IMAGE_ID"; then
    pass_check cold_config local_build_config_safe_and_frozen
else
    fatal_check cold_config local_build_config_rejected_or_not_frozen
fi

if ! execution_inputs_unchanged; then
    fatal_check private_env private_input_changed_before_start
fi
STACK_STARTED=1
if compose_local up -d --no-build --pull never --force-recreate \
    sandbox egress-proxy relay >/dev/null 2>&1; then
    pass_check cold_start base_services_created
else
    fatal_check cold_start base_service_start_failed
fi

if wait_healthy sandbox && wait_healthy egress-proxy && wait_healthy relay; then
    pass_check base_health sandbox_proxy_relay_healthy
else
    fatal_check base_health base_service_unhealthy
fi

if compose_local exec -T egress-proxy python -I -c '
from pathlib import Path
lines = [line.strip() for line in Path("/etc/vdc/egress-hosts.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
raise SystemExit(0 if lines == ["replace.invalid"] else 1)
' >/dev/null 2>&1; then
    pass_check runtime_policy deny_only_mount_confirmed
else
    fatal_check runtime_policy mounted_policy_mismatch
fi

if compose_local run --rm --no-deps --entrypoint python worker -I -c '
from pathlib import Path
from video_download_control.database import SCHEMA_VERSION, Database

database = Database(Path("/var/lib/vdc/control.sqlite3"))
database.initialize()
with database.connect() as connection:
    count = connection.execute("SELECT COUNT(*) FROM download_jobs").fetchone()[0]
ready, _ = database.readiness()
raise SystemExit(0 if SCHEMA_VERSION == 11 and ready and int(count) == 0 else 1)
' >/dev/null 2>&1; then
    pass_check empty_database schema11_empty_database_ready
else
    fatal_check empty_database dedicated_database_not_empty
fi

if ! execution_inputs_unchanged; then
    fatal_check private_env private_input_changed_before_worker_start
fi
if compose_local up -d --no-build --pull never worker >/dev/null 2>&1; then
    pass_check worker_start candidate_worker_created
else
    fatal_check worker_start candidate_worker_start_failed
fi
if wait_healthy worker; then
    pass_check service_health all_four_services_healthy
else
    fatal_check service_health worker_unhealthy
fi

runtime_ok=1
for service_name in sandbox egress-proxy relay worker; do
    container_id=$(container_id_for "$service_name") || runtime_ok=0
    case "$container_id" in
        ''|*[!0-9a-f]*) runtime_ok=0 ;;
        *)
            if ! docker inspect "$container_id" 2>/dev/null \
                | validate_runtime_container "$BUILT_IMAGE_ID" >/dev/null 2>&1; then
                runtime_ok=0
            fi
            ;;
    esac
done
if [ "$runtime_ok" -eq 1 ]; then
    pass_check runtime_hardening readonly_caps_and_resources_enforced
else
    fatal_check runtime_hardening runtime_boundary_mismatch
fi

SANDBOX_ID=$(container_id_for sandbox) || fatal_check runtime_namespace sandbox_container_missing
EGRESS_ID=$(container_id_for egress-proxy) || fatal_check runtime_namespace proxy_container_missing
RELAY_ID=$(container_id_for relay) || fatal_check runtime_namespace relay_container_missing
WORKER_ID=$(container_id_for worker) || fatal_check runtime_namespace worker_container_missing
if docker inspect "$WORKER_ID" 2>/dev/null | "$PYTHON_BIN" -I -c '
import json
import sys

payload = json.load(sys.stdin)
assert len(payload) == 1
limits = payload[0]["HostConfig"].get("Ulimits") or []
core = [item for item in limits if item.get("Name") == "core"]
assert len(core) == 1
assert int(core[0].get("Soft", -1)) == 0
assert int(core[0].get("Hard", -1)) == 0
' >/dev/null 2>&1 \
    && compose_local exec -T worker python -I -c '
import resource

raise SystemExit(0 if resource.getrlimit(resource.RLIMIT_CORE) == (0, 0) else 1)
' >/dev/null 2>&1; then
    pass_check worker_core_dump core_soft_and_hard_limits_zero
else
    fatal_check worker_core_dump core_dump_limit_not_disabled
fi
if docker inspect "$SANDBOX_ID" "$EGRESS_ID" "$RELAY_ID" "$WORKER_ID" 2>/dev/null \
    | "$PYTHON_BIN" -I -c '
import json
import sys

sandbox_id, egress_id, relay_id, worker_id = sys.argv[1:]
containers = {item["Id"]: item for item in json.load(sys.stdin)}
assert set(containers) == {sandbox_id, egress_id, relay_id, worker_id}
assert containers[sandbox_id]["HostConfig"]["NetworkMode"] == "none"
expected_shared = f"container:{sandbox_id}"
assert containers[relay_id]["HostConfig"]["NetworkMode"] == expected_shared
assert containers[worker_id]["HostConfig"]["NetworkMode"] == expected_shared
egress_mode = containers[egress_id]["HostConfig"]["NetworkMode"]
assert egress_mode not in {"host", "none", expected_shared}
assert not str(egress_mode).startswith("container:")
' "$SANDBOX_ID" "$EGRESS_ID" "$RELAY_ID" "$WORKER_ID" >/dev/null 2>&1
then
    pass_check runtime_namespace only_proxy_has_bridge_worker_and_relay_share_none
else
    fatal_check runtime_namespace runtime_network_mode_mismatch
fi

if compose_local exec -T worker python -I -c '
from pathlib import Path
from video_download_control.security import UnixRelayNetworkGuard

UnixRelayNetworkGuard(
    unix_socket_path=Path("/run/vdc-egress/proxy.sock"),
    relay_host="127.0.0.1",
    relay_port=18080,
).assert_ready(adapter_name="acceptance")
' >/dev/null 2>&1; then
    pass_check namespace_uds loopback_routes_socket_and_relay_ready
else
    fatal_check namespace_uds isolation_guard_failed
fi

if compose_local exec -T worker python -I -c '
import socket

def denied(authority: str) -> bool:
    with socket.create_connection(("127.0.0.1", 18080), timeout=3) as connection:
        request = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
        connection.sendall(request)
        response = connection.recv(256)
    fields = response.split(b" ", 2)
    return len(fields) >= 2 and fields[1].isdigit() and 400 <= int(fields[1]) < 500

targets = ("127.0.0.1:443", "10.0.0.1:443", "169.254.169.254:80")
raise SystemExit(0 if all(denied(target) for target in targets) else 1)
' >/dev/null 2>&1 \
    && compose_local exec -T worker python -I -c '
from video_download_control.security import EgressPolicyError, resolve_public_target

try:
    resolve_public_target(
        "https://fixture.invalid/",
        resolver=lambda host, port: ("127.0.0.1",),
        allowed_hosts=("fixture.invalid",),
    )
except EgressPolicyError as exc:
    raise SystemExit(0 if exc.reason == "non_public_address" else 1)
raise SystemExit(1)
' >/dev/null 2>&1; then
    pass_check ssrf_policy live_rejections_and_private_dns_guard
else
    fatal_check ssrf_policy ssrf_guard_failed
fi

if compose_local exec -T worker python -I -c '
from video_download_control.security import EgressPolicyError, ResolvedTarget, assert_connected_peer

target = ResolvedTarget(
    url="https://fixture.invalid/",
    scheme="https",
    host="fixture.invalid",
    port=443,
    addresses=("8.8.8.8",),
)
try:
    assert_connected_peer(target, "1.1.1.1")
except EgressPolicyError as exc:
    raise SystemExit(0 if exc.reason == "peer_address_mismatch" else 1)
raise SystemExit(1)
' >/dev/null 2>&1; then
    pass_check peer_attestation mismatched_peer_rejected
else
    fatal_check peer_attestation peer_guard_failed
fi

if docker inspect "$WORKER_ID" 2>/dev/null | "$PYTHON_BIN" -I -c '
import json
import sys

cookie_root, mapping_file = sys.argv[1:]
payload = json.load(sys.stdin)
assert len(payload) == 1
mounts = {
    item.get("Destination"): item
    for item in payload[0].get("Mounts", [])
    if item.get("Destination") in {
        "/run/vdc-cookie-sources",
        "/run/vdc-cookie-mapping.env",
    }
}
assert set(mounts) == {
    "/run/vdc-cookie-sources",
    "/run/vdc-cookie-mapping.env",
}
expected_sources = {
    "/run/vdc-cookie-sources": cookie_root,
    "/run/vdc-cookie-mapping.env": mapping_file,
}
for destination, item in mounts.items():
    assert item.get("Type") == "bind"
    assert item.get("Source") == expected_sources[destination]
    assert item.get("RW") is False
' "$COOKIE_ROOT_SOURCE" "$COOKIE_MAPPING_SOURCE" >/dev/null 2>&1 \
    && compose_local exec -T worker python -I -c '
import os
import stat
from pathlib import Path

workers = []
for process in Path("/proc").iterdir():
    if not process.name.isdigit():
        continue
    try:
        arguments = [
            item.decode("utf-8", errors="strict")
            for item in (process / "cmdline").read_bytes().split(b"\0")
            if item
        ]
        executable = (process / "exe").resolve(strict=True).name
    except (OSError, RuntimeError, UnicodeError):
        continue
    if executable.startswith("python") and any(
        Path(item).name == "video-download-candidate-worker"
        for item in arguments
    ):
        workers.append(arguments)
assert len(workers) == 1
arguments = workers[0]
specs = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--cookie-source"]
assert len(specs) == 6
platforms = set()
references = set()
paths = set()
identities = set()
for spec in specs:
    identity, raw_path = spec.split("=", 1)
    platform, opaque_ref = identity.split(":", 1)
    assert opaque_ref.startswith("acceptance-")
    assert opaque_ref not in references
    path = Path(raw_path)
    assert path not in paths
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert not stat.S_ISLNK(info.st_mode)
    assert info.st_uid == 0 and info.st_gid == 10001
    assert stat.S_IMODE(info.st_mode) == 0o440
    assert info.st_nlink == 1
    assert not info.st_mode & 0o222
    identity = (info.st_dev, info.st_ino)
    assert identity not in identities
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    os.close(descriptor)
    try:
        descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        pass
    else:
        os.close(descriptor)
        raise AssertionError("cookie mount is writable")
    platforms.add(platform)
    references.add(opaque_ref)
    paths.add(path)
    identities.add(identity)
raise SystemExit(
    0
    if platforms == {"x", "youtube", "bilibili", "douyin", "tiktok", "instagram"}
    else 1
)
' >/dev/null 2>&1; then
    pass_check cookie_mount_permissions synthetic_sources_readonly_and_private
else
    fatal_check cookie_mount_permissions cookie_runtime_contract_failed
fi

if ! compose_local stop -t 30 worker >/dev/null 2>&1; then
    fatal_check worker_sigterm worker_stop_command_failed
fi
if docker inspect "$WORKER_ID" 2>/dev/null | "$PYTHON_BIN" -I -c '
import json
import sys

payload = json.load(sys.stdin)
assert len(payload) == 1
state = payload[0]["State"]
assert state.get("Running") is False
assert state.get("OOMKilled") is False
assert int(state.get("ExitCode")) in {0, 143}
' >/dev/null 2>&1; then
    pass_check worker_sigterm idle_worker_exited_without_forced_kill
else
    fatal_check worker_sigterm worker_exit_state_rejected
fi

LEASE_CONTAINER=$(docker run -d --rm \
    "${RUN_SANDBOX_ARGS[@]}" \
    --init \
    --user 10001:10001 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700 \
    --mount "type=bind,source=$DATA_SOURCE,target=/var/lib/vdc" \
    "$BUILT_IMAGE_ID" \
    python -I -c '
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from video_download_control.database import Database
from video_download_control.repository import BatchRepository
from video_download_control.service import BatchService
from video_download_control.worker_repository import WorkerRepository

database = Database(Path("/var/lib/vdc/control.sqlite3"))
service = BatchService(BatchRepository(database), 50, "acceptance-v1")
service.create_batch(name="linux-acceptance-lease-fixture", raw_inputs=["https://youtu.be/AcptLease01"])
lease = WorkerRepository(database).claim_next(
    worker_id="acceptance-sigterm",
    adapter="acceptance-fixture",
    adapter_version="1",
    now=datetime.now(UTC),
    lease_seconds=10,
)
assert lease is not None
Path("/tmp/vdc-lease-ready").touch()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(1)
' 2>/dev/null) || fatal_check sigterm_lease_recovery fixture_container_start_failed
case "$LEASE_CONTAINER" in
    ''|*[!0-9a-f]*) fatal_check sigterm_lease_recovery invalid_fixture_container ;;
esac

lease_ready=0
for _ in {1..20}; do
    if docker exec "$LEASE_CONTAINER" python -I -c 'from pathlib import Path; raise SystemExit(0 if Path("/tmp/vdc-lease-ready").is_file() else 1)' >/dev/null 2>&1; then
        lease_ready=1
        break
    fi
    sleep 1
done
if [ "$lease_ready" -ne 1 ]; then
    fatal_check sigterm_lease_recovery fixture_lease_not_ready
fi
if ! docker kill --signal TERM "$LEASE_CONTAINER" >/dev/null 2>&1; then
    fatal_check sigterm_lease_recovery fixture_sigterm_failed
fi
lease_stopped=0
for _ in {1..15}; do
    if ! docker inspect "$LEASE_CONTAINER" >/dev/null 2>&1; then
        lease_stopped=1
        break
    fi
    sleep 1
done
if [ "$lease_stopped" -ne 1 ]; then
    fatal_check sigterm_lease_recovery fixture_did_not_exit
fi
LEASE_CONTAINER=
sleep 12

if docker run --rm \
    "${RUN_SANDBOX_ARGS[@]}" \
    --user 10001:10001 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700 \
    --mount "type=bind,source=$DATA_SOURCE,target=/var/lib/vdc" \
    "$BUILT_IMAGE_ID" \
    python -I -c '
from datetime import UTC, datetime
from pathlib import Path
from video_download_control.database import Database
from video_download_control.worker_repository import WorkerRepository

database = Database(Path("/var/lib/vdc/control.sqlite3"))
with database.connect() as connection:
    row = connection.execute("""
        SELECT job.id
        FROM download_jobs AS job
        JOIN batches AS batch ON batch.id = job.batch_id
        WHERE batch.name = ?
        ORDER BY job.created_at DESC, job.id DESC
        LIMIT 1
    """, ("linux-acceptance-lease-fixture",)).fetchone()
assert row is not None
repository = WorkerRepository(database)
lease = repository.claim_next(
    worker_id="acceptance-recovery",
    adapter="acceptance-fixture",
    adapter_version="1",
    now=datetime.now(UTC),
    lease_seconds=10,
)
assert lease is not None
assert lease.job_id == row["id"]
assert lease.attempt_no == 2
with database.connect() as connection:
    abandoned = connection.execute(
        "SELECT COUNT(*) FROM job_attempts WHERE job_id = ? AND status = ?",
        (lease.job_id, "abandoned"),
    ).fetchone()[0]
    current_generation_attempt = connection.execute(
        "SELECT generation_attempt_no FROM job_attempts WHERE id = ?",
        (lease.attempt_id,),
    ).fetchone()[0]
assert int(abandoned) == 1
assert int(current_generation_attempt) == 2
repository.finish_canceled(lease, now=datetime.now(UTC))
' >/dev/null 2>&1; then
    pass_check sigterm_lease_recovery expired_lease_abandoned_and_reclaimed
else
    fatal_check sigterm_lease_recovery lease_recovery_failed
fi

if ! execution_inputs_unchanged; then
    fatal_check private_env private_input_changed_before_worker_restart
fi
if compose_local start worker >/dev/null 2>&1 && wait_healthy worker; then
    pass_check worker_restart worker_healthy_after_fixture
else
    fatal_check worker_restart worker_restart_failed
fi

if docker run --rm \
    "${RUN_SANDBOX_ARGS[@]}" \
    --user 10001:10001 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700 \
    --mount "type=bind,source=$BACKUP_ROOT,target=/acceptance-backup,readonly" \
    --mount "type=bind,source=$RESTORE_PARENT,target=/acceptance-output" \
    "$BUILT_IMAGE_ID" \
    video-download-backup restore \
    --backup-root /acceptance-backup \
    --restore-data-root "/acceptance-output/$RESTORE_NAME" \
    --restore-database "/acceptance-output/$RESTORE_NAME/control.sqlite3" \
    >/dev/null 2>&1 \
    && docker run --rm \
        "${RUN_SANDBOX_ARGS[@]}" \
        --user 10001:10001 \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700 \
        --mount "type=bind,source=$RESTORE_TARGET,target=/acceptance-restore,readonly" \
        "$BUILT_IMAGE_ID" \
        python -I -c '
from pathlib import Path
from video_download_control.database import SCHEMA_VERSION, Database

database = Database(Path("/acceptance-restore/control.sqlite3"))
ready, _ = database.readiness()
raise SystemExit(0 if SCHEMA_VERSION == 11 and ready else 1)
' >/dev/null 2>&1; then
    pass_check schema11_restore independent_restore_ready_and_retained
else
    fatal_check schema11_restore restore_or_readiness_failed
fi

if ! compose_local stop -t 30 worker relay egress-proxy >/dev/null 2>&1; then
    fatal_check stale_socket graceful_service_stop_failed
fi
if [ -e "$SOCKET_SOURCE/proxy.sock" ] || [ -L "$SOCKET_SOURCE/proxy.sock" ]; then
    fatal_check stale_socket graceful_close_left_socket
fi

if ! docker run --rm \
    "${RUN_SANDBOX_ARGS[@]}" \
    --user 10001:10001 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700 \
    --mount "type=bind,source=$SOCKET_SOURCE,target=/run/vdc-egress" \
    "$BUILT_IMAGE_ID" \
    python -I -c '
import os
import socket

path = "/run/vdc-egress/proxy.sock"
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.close()
os.chmod(path, 0o660, follow_symlinks=False)
' >/dev/null 2>&1; then
    fatal_check stale_socket stale_fixture_creation_failed
fi
if [ ! -S "$SOCKET_SOURCE/proxy.sock" ]; then
    fatal_check stale_socket stale_fixture_not_socket
fi
stale_identity=$(stat -Lc '%d:%i' -- "$SOCKET_SOURCE/proxy.sock" 2>/dev/null) || fatal_check stale_socket stale_identity_unavailable

if ! execution_inputs_unchanged; then
    fatal_check private_env private_input_changed_before_proxy_restart
fi
if ! compose_local up -d --no-build --pull never egress-proxy >/dev/null 2>&1; then
    fatal_check stale_socket proxy_restart_command_failed
fi
sleep 8
proxy_id=$(compose_local ps -a -q egress-proxy 2>/dev/null) || fatal_check stale_socket proxy_container_missing
case "$proxy_id" in
    ''|*[!0-9a-f]*) fatal_check stale_socket invalid_proxy_container ;;
esac
if ! docker inspect "$proxy_id" 2>/dev/null | "$PYTHON_BIN" -I -c '
import json
import sys

payload = json.load(sys.stdin)
assert len(payload) == 1
container = payload[0]
health = (container["State"].get("Health") or {}).get("Status", "none")
status = container["State"].get("Status")
assert health != "healthy"
assert int(container.get("RestartCount") or 0) >= 1 or status in {"exited", "dead"}
' >/dev/null 2>&1; then
    fatal_check stale_socket proxy_did_not_attempt_and_refuse_stale_socket
fi
current_identity=$(stat -Lc '%d:%i' -- "$SOCKET_SOURCE/proxy.sock" 2>/dev/null) || fatal_check stale_socket stale_identity_lost
if [ "$current_identity" != "$stale_identity" ]; then
    fatal_check stale_socket stale_socket_replaced_or_accepted
fi
compose_local stop -t 15 egress-proxy >/dev/null 2>&1 || :
if ! compose_local down --timeout 30 >/dev/null 2>&1; then
    fatal_check stack_shutdown acceptance_project_shutdown_failed
fi
STACK_STARTED=0
pass_check stack_shutdown acceptance_project_removed
if [ ! -S "$SOCKET_SOURCE/proxy.sock" ]; then
    fatal_check stale_socket stale_socket_not_retained
fi
pass_check stale_socket refused_without_replacement_manual_cleanup_required

pass_check audit_redaction fixed_fields_only_no_command_output
emit summary pass failures_0_blocked_0
exit 0
