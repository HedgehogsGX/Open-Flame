from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "deployment" / "run-linux-acceptance.sh"
CHECKLIST = ROOT / "validation" / "linux-docker-acceptance.md"


def find_bash() -> str | None:
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    for candidate in (
        Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Git" / "usr" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


BASH = find_bash()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_linux_acceptance_assets_exist_and_are_cross_linked() -> None:
    runner = read(RUNNER)
    checklist = read(CHECKLIST)

    assert runner.startswith("#!/bin/bash -p\n")
    assert "case $- in" in runner
    assert "runner requires a direct shebang launch or /bin/bash -p" in runner
    assert "unset BASH_ENV ENV POSIXLY_CORRECT" in runner
    assert "PYTHONOPTIMIZE" in runner
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in runner
    assert "set -eu\nset -o pipefail\n" in runner
    assert "[`deployment/run-linux-acceptance.sh`]" in checklist
    assert "../deployment/run-linux-acceptance.sh" in checklist
    assert "not" in checklist.lower() and "current Windows" in checklist

    relative_links = re.findall(r"\]\(([^)]+)\)", checklist)
    assert relative_links
    for raw_target in relative_links:
        path_part = raw_target.split("#", 1)[0]
        if not path_part or "://" in path_part:
            continue
        assert (CHECKLIST.parent / path_part).resolve().is_file(), raw_target


def test_runner_uses_fixed_repository_paths_and_defaults_to_preflight() -> None:
    runner = read(RUNNER)

    for contract in (
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)',
        'REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)',
        'COMPOSE_FILE="$REPO_ROOT/deployment/compose.candidate.yaml"',
        'DOCKERFILE="$REPO_ROOT/deployment/Dockerfile.candidate"',
        'BUILD_REQUIREMENTS="$REPO_ROOT/deployment/requirements.build.lock"',
        'RUNTIME_REQUIREMENTS="$REPO_ROOT/deployment/requirements.runtime.lock"',
        "MODE=preflight",
        "AUTHORIZATION_TOKEN=I_ACCEPT_TARGET_LINUX_MUTATIONS",
        'if [ "$MODE" = preflight ]; then',
        'if [ "$AUTHORIZATION" != "$AUTHORIZATION_TOKEN" ]; then',
    ):
        assert contract in runner

    authorization = runner.index(
        'if [ "$AUTHORIZATION" != "$AUTHORIZATION_TOKEN" ]; then'
    )
    for mutation in (
        "if docker build \\",
        "if compose_local up -d",
        "server.bind(path)",
        "video-download-backup restore",
    ):
        assert authorization < runner.index(mutation)

    assert "${DOCKER_HOST:-$context_endpoint}" not in runner
    context_branch = runner.index('if [ -n "${DOCKER_CONTEXT:-}" ]; then')
    host_branch = runner.index('elif [ -n "${DOCKER_HOST:-}" ]; then')
    assert context_branch < host_branch
    for control_name in (
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "BUILDKIT_HOST",
        "BUILDX_BUILDER",
        "COMPOSE_FILE",
    ):
        assert f"    {control_name} \\" in runner

    assert "PYTHON_BIN=/usr/bin/python3" in runner
    assert "command -v python3" not in runner
    assert "sys.version_info >= (3, 12)" in runner
    assert 'if [ "$EUID" -ne 0 ]; then' in runner


def test_linux_acceptance_assets_target_v018_schema_11_contract() -> None:
    runner = read(RUNNER)
    checklist = read(CHECKLIST)

    for contract in (
        'RESTORE_NAME="vdc-schema11-acceptance-$RUN_ID"',
        "--backup-root ABSOLUTE_SCHEMA11_BACKUP_DIR",
        '"video-download-control": "0.24.4"',
        "SCHEMA_VERSION == 11",
        "schema11_exact_runtime_lock_and_no_real_exact_selector",
        "schema11_empty_database_ready",
        "schema11_restore independent_restore_ready_and_retained",
    ):
        assert contract in runner

    assert "schema10_restore" not in runner
    assert "ABSOLUTE_SCHEMA9_BACKUP_DIR" not in runner
    assert checklist.startswith("# Iteration 0.24.4 target Linux/Docker acceptance")
    for contract in (
        "Schema 11 backup",
        "video-download-control==0.24.4",
        "confirms Schema 11",
        "Schema 11 restore",
        "`schema11_restore`",
        "Schema version 11 readiness",
        "/absolute/read-only/schema11-backup",
    ):
        assert contract in checklist

    # Older versions remain relevant only as an explicit forward-migration path.
    assert "v0.15" in checklist
    assert "Schema 10" in checklist
    assert "re-backed up as Schema 11" in checklist


def test_runner_freezes_the_validated_effective_compose_model_before_start() -> None:
    runner = read(RUNNER)

    for contract in (
        'FROZEN_COMPOSE_FILE="$frozen_parent/.vdc-effective-$RUN_ID.json"',
        "set -C",
        'validate_private_input_metadata "$FROZEN_COMPOSE_FILE"',
        "FROZEN_COMPOSE_SNAPSHOT=$PRIVATE_INPUT_SNAPSHOT",
        '-f "$FROZEN_COMPOSE_FILE"',
        'assert "$" not in value',
        "assert actual == expected",
        "local_build_config_safe_and_frozen",
        'rm -f -- "$FROZEN_COMPOSE_FILE"',
    ):
        assert contract in runner

    freeze = runner.index('if freeze_effective_config "$BUILT_IMAGE_ID"; then')
    first_start = runner.index(
        "if compose_local up -d --no-build --pull never --force-recreate"
    )
    assert freeze < first_start
    capture = runner.index("BUILT_IMAGE_ID=$(docker image inspect")
    assert '[[ ! "$BUILT_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]' in runner
    assert 'validate_runtime_container "$BUILT_IMAGE_ID"' in runner
    assert '    "$BUILD_TAG" \\' not in runner[capture:]


def test_frozen_compose_guards_execute_as_pure_python_contracts() -> None:
    runner = read(RUNNER)
    freeze_start = runner.index("freeze_effective_config() {")
    python_marker = '"$PYTHON_BIN" -I -c \'\n'

    dollar_start = runner.index(python_marker, freeze_start) + len(python_marker)
    dollar_end = runner.index("\n' >/dev/null 2>&1 || return 1", dollar_start)
    dollar_guard = runner[dollar_start:dollar_end]

    safe = subprocess.run(
        [sys.executable, "-I", "-c", dollar_guard],
        input=json.dumps({"services": {"worker": ["literal", 1]}}),
        check=False,
        capture_output=True,
        text=True,
    )
    unsafe = subprocess.run(
        [sys.executable, "-I", "-c", dollar_guard],
        input=json.dumps({"services": {"worker": "${UNTRUSTED}"}}),
        check=False,
        capture_output=True,
        text=True,
    )
    assert safe.returncode == 0
    assert unsafe.returncode != 0

    equivalence_anchor = runner.index("frozen_json=", dollar_end)
    equivalence_start = runner.index(python_marker, equivalence_anchor) + len(
        python_marker
    )
    equivalence_end = runner.index("\n' >/dev/null 2>&1 || return 1", equivalence_start)
    equivalence_guard = runner[equivalence_start:equivalence_end]
    expected = {"name": "vdc", "services": {"worker": {"image": "candidate"}}}

    equal = subprocess.run(
        [sys.executable, "-I", "-c", equivalence_guard],
        input=f"{json.dumps(expected)}\n{json.dumps(expected)}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    changed = subprocess.run(
        [sys.executable, "-I", "-c", equivalence_guard],
        input=f"{json.dumps(expected)}\n{json.dumps({'name': 'other'})}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert equal.returncode == 0
    assert changed.returncode != 0


def test_cookie_validator_runs_in_an_explicitly_empty_child_environment() -> None:
    runner = read(RUNNER)
    function_start = runner.index("run_cookie_validator() {")
    function_end = runner.index("\n}\n", function_start)
    launcher = runner[function_start:function_end]

    assert "/usr/bin/env -i" in launcher
    assert '/bin/sh "$COOKIE_VALIDATOR"' in launcher
    assert "HOME=" not in launcher
    assert "TMPDIR=" not in launcher
    assert "BASH_FUNC" not in launcher
    for variable in (
        "VDC_COOKIE_SOURCE_ROOT",
        "VDC_COOKIE_MAPPING_FILE",
        "VDC_DATA_ROOT",
        "VDC_SOCKET_ROOT",
        "VDC_COOKIE_STAGING_PLATFORM",
    ):
        assert f'"{variable}=' in launcher
    assert runner.count('run_cookie_validator ""') == 2
    assert '\n        sh "$COOKIE_VALIDATOR"' not in runner


def test_runner_covers_each_required_target_acceptance_area() -> None:
    runner = read(RUNNER)

    required_checks = {
        "compose_config",
        "scoped_project",
        "root_execution",
        "image_build",
        "image_identity",
        "image_runtime_contract",
        "host_coredump_policy",
        "cold_start",
        "base_health",
        "service_health",
        "runtime_hardening",
        "worker_core_dump",
        "runtime_namespace",
        "namespace_uds",
        "ssrf_policy",
        "peer_attestation",
        "cookie_host_metadata",
        "cookie_mount_permissions",
        "worker_sigterm",
        "sigterm_lease_recovery",
        "schema11_restore",
        "stack_shutdown",
        "stale_socket",
        "audit_redaction",
    }
    emitted_checks = set(
        re.findall(
            r"^\s*(?:pass_check|fail_check|block_check|fatal_check) "
            r"([a-z0-9_]+)",
            runner,
            flags=re.MULTILINE,
        )
    )
    assert required_checks <= emitted_checks

    for runtime_contract in (
        "--format '{{if .State.Health}}",
        'assert config.get("User") == "10001:10001"',
        'assert container["Image"] == sys.argv[1]',
        'assert host.get("ReadonlyRootfs") is True',
        'assert host.get("Init") is True',
        'assert "ALL" in (host.get("CapDrop") or [])',
        'core_limit = (worker.get("ulimits") or {}).get("core") or {}',
        "resource.RLIMIT_CORE",
        "freeze_effective_config",
        "PackageNotFoundError",
        "/proc/sys/kernel/core_pattern",
        "core_soft_and_hard_limits_zero",
        'expected_shared = f"container:{sandbox_id}"',
        "UnixRelayNetworkGuard(",
        '"169.254.169.254:80"',
        "assert_connected_peer",
        "assert len(specs) == 6",
        'item.get("RW") is False',
        'item.get("Destination")',
        "docker kill --signal TERM",
        "lease.attempt_no == 2",
        "SCHEMA_VERSION == 11 and ready",
        "stale_identity",
    ):
        assert runtime_contract in runner


def test_every_inline_python_program_compiles() -> None:
    runner = read(RUNNER)
    programs = re.findall(
        r'(?:"\$PYTHON_BIN"|python|--entrypoint python worker)\s+-I\s+-c\s+\'([^\']*)\'',
        runner,
        flags=re.DOTALL,
    )
    programs.extend(re.findall(r"<<'PY'\n(.*?)\nPY", runner, flags=re.DOTALL))

    assert len(programs) >= 15
    for index, program in enumerate(programs):
        compile(program, f"run-linux-acceptance.sh:inline-{index}", "exec")


def test_embedded_compose_validator_accepts_optional_wrapper_contract() -> None:
    runner = read(RUNNER)
    function_start = runner.index("validate_config_json() {")
    program_start = runner.index('"$PYTHON_BIN" -I -c \'\n', function_start) + len(
        '"$PYTHON_BIN" -I -c \'\n'
    )
    program_end = runner.index(
        '\n\' "$require_digest" "$require_cookie" "$execution_mode" "$COOKIE_RUNNER"',
        program_start,
    )
    validator = runner[program_start:program_end]
    expected_runner = "/reviewed/repository/deployment/cookies/run-candidate-worker.sh"

    def validate(
        document: dict[str, object],
        *,
        require_cookie: bool,
        require_digest: bool = True,
        baseline: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        baseline = baseline or baseline_document
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                validator,
                "1" if require_digest else "0",
                "1" if require_cookie else "0",
                "1",
                expected_runner,
            ],
            input=json.dumps(baseline) + "\n" + json.dumps(document),
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONOPTIMIZE": "1",
                "PYTHONPATH": "/untrusted/python/path",
            },
        )

    image = "registry.invalid/candidate@sha256:" + "1" * 64
    common = {
        "image": image,
        "pull_policy": "never",
        "restart": "unless-stopped",
        "profiles": ["candidate-real-worker"],
        "user": "10001:10001",
        "read_only": True,
        "init": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 32,
        "mem_limit": "64m",
        "cpus": 0.25,
        "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700"],
        "healthcheck": {"test": ["CMD", "true"]},
    }

    def bind(source: str, target: str, *, read_only: bool) -> dict[str, object]:
        return {
            "type": "bind",
            "source": source,
            "target": target,
            "read_only": read_only,
            "bind": {"create_host_path": False},
        }

    socket_bind = "/srv/acceptance/socket"
    data_volume = bind("/srv/acceptance/data", "/var/lib/vdc", read_only=False)
    worker_socket_volume = bind(socket_bind, "/run/vdc-egress", read_only=True)
    worker_command = [
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
        "2026.08.31",
        "--ffmpeg-version",
        "7.1",
        "--ffprobe-version",
        "7.1",
        "--egress-policy-version",
        "acceptance-v1",
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
    document = {
        "services": {
            "sandbox": {
                **common,
                "network_mode": "none",
                "command": [
                    "python",
                    "-I",
                    "-c",
                    (
                        "import signal,threading; stopped=threading.Event(); "
                        "signal.signal(signal.SIGTERM,lambda *_:stopped.set()); "
                        "signal.signal(signal.SIGINT,lambda *_:stopped.set()); "
                        "stopped.wait()"
                    ),
                ],
            },
            "egress-proxy": {
                **common,
                "tmpfs": [
                    "/tmp:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700"
                ],
                "networks": {"egress": {}},
                "command": [
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
                ],
                "volumes": [
                    bind(socket_bind, "/run/vdc-egress", read_only=False),
                    bind(
                        "/srv/acceptance/policy.txt",
                        "/etc/vdc/egress-hosts.txt",
                        read_only=True,
                    ),
                ],
            },
            "relay": {
                **common,
                "network_mode": "service:sandbox",
                "command": [
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
                ],
                "volumes": [bind(socket_bind, "/run/vdc-egress", read_only=True)],
            },
            "worker": {
                **common,
                "tmpfs": [
                    "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700"
                ],
                "network_mode": "service:sandbox",
                "environment": {"VDC_ENABLE_CANDIDATE_REAL_WORKER": "1"},
                "ulimits": {"core": {"soft": 0, "hard": 0}},
                "entrypoint": ["/bin/sh", "/run/vdc-cookie-runner.sh"],
                "command": worker_command,
                "volumes": [
                    data_volume,
                    worker_socket_volume,
                    bind(
                        "/srv/acceptance/cookies",
                        "/run/vdc-cookie-sources",
                        read_only=True,
                    ),
                    bind(
                        "/srv/acceptance/mapping.env",
                        "/run/vdc-cookie-mapping.env",
                        read_only=True,
                    ),
                ],
                "configs": [
                    {
                        "source": "cookie-runner",
                        "target": "/run/vdc-cookie-runner.sh",
                        "mode": 292,
                    }
                ],
            },
        },
        "configs": {"cookie-runner": {"file": expected_runner}},
        "networks": {"egress": {"driver": "bridge"}},
    }
    baseline_document = json.loads(json.dumps(document))
    baseline_worker = baseline_document["services"]["worker"]
    baseline_worker.pop("entrypoint")
    baseline_worker.pop("configs")
    baseline_worker["volumes"] = [data_volume, worker_socket_volume]
    baseline_document.pop("configs")
    completed = validate(document, require_cookie=True)

    assert completed.returncode == 0, completed.stderr

    local_image_id = "sha256:" + "2" * 64
    image_id_document = json.loads(json.dumps(document))
    image_id_baseline = json.loads(json.dumps(baseline_document))
    for model in (image_id_document, image_id_baseline):
        for service in model["services"].values():
            service["image"] = local_image_id
    image_id_result = validate(
        image_id_document,
        require_cookie=True,
        require_digest=False,
        baseline=image_id_baseline,
    )
    assert image_id_result.returncode == 0, image_id_result.stderr
    registry_only = validate(
        image_id_document,
        require_cookie=True,
        require_digest=True,
        baseline=image_id_baseline,
    )
    assert registry_only.returncode != 0

    zero_source_document = json.loads(json.dumps(document))
    zero_source_worker = zero_source_document["services"]["worker"]
    zero_source_worker.pop("entrypoint")
    zero_source_worker.pop("configs")
    zero_source_worker["volumes"] = [data_volume, worker_socket_volume]
    zero_source_document.pop("configs")
    zero_source = validate(zero_source_document, require_cookie=False)
    assert zero_source.returncode == 0, zero_source.stderr

    for invalid_core in (None, {"core": {"soft": 1, "hard": 1}}):
        unsafe_core_document = json.loads(json.dumps(document))
        if invalid_core is None:
            unsafe_core_document["services"]["worker"].pop("ulimits")
        else:
            unsafe_core_document["services"]["worker"]["ulimits"] = invalid_core
        unsafe_core = validate(unsafe_core_document, require_cookie=True)
        assert unsafe_core.returncode != 0

    wrong_runner_document = json.loads(json.dumps(document))
    wrong_runner_document["configs"]["cookie-runner"]["file"] = (
        "/private/unreviewed-runner.sh"
    )
    wrong_runner = validate(wrong_runner_document, require_cookie=True)
    assert wrong_runner.returncode != 0

    secret_injection_document = json.loads(json.dumps(document))
    secret_injection_document["services"]["relay"]["secrets"] = [
        {"source": "cookie-secret", "target": "/run/hidden-cookie"}
    ]
    secret_injection_document["secrets"] = {
        "cookie-secret": {"file": "/private/cookie.txt"}
    }
    secret_injection = validate(secret_injection_document, require_cookie=True)
    assert secret_injection.returncode != 0

    config_injection_document = json.loads(json.dumps(document))
    config_injection_document["services"]["sandbox"]["configs"] = [
        {"source": "cookie-runner", "target": "/run/also-here"}
    ]
    config_injection = validate(config_injection_document, require_cookie=True)
    assert config_injection.returncode != 0

    hostile_documents: list[dict[str, object]] = []

    extra_allow_host = json.loads(json.dumps(document))
    extra_allow_host["services"]["egress-proxy"]["command"].extend(
        ["--allowed-host", "example.com"]
    )
    hostile_documents.append(extra_allow_host)

    leaking_healthcheck = json.loads(json.dumps(document))
    leaking_healthcheck["services"]["worker"]["healthcheck"]["test"] = [
        "CMD-SHELL",
        "cat /run/vdc-cookie-sources/youtube/cookies.txt",
    ]
    hostile_documents.append(leaking_healthcheck)

    conflicting_tmpfs = json.loads(json.dumps(document))
    conflicting_tmpfs["services"]["worker"]["tmpfs"] = [
        "/tmp:rw,noexec,exec,nosuid,nodev,size=64m,uid=10001,uid=0,gid=10001,mode=0700,mode=0777"
    ]
    hostile_documents.append(conflicting_tmpfs)

    bind_propagation = json.loads(json.dumps(document))
    bind_propagation["services"]["worker"]["volumes"][0]["bind"]["propagation"] = (
        "rshared"
    )
    hostile_documents.append(bind_propagation)

    lifecycle_hook = json.loads(json.dumps(document))
    lifecycle_hook["services"]["worker"]["post_start"] = [
        {"command": ["/bin/sh", "-c", "true"], "privileged": True}
    ]
    hostile_documents.append(lifecycle_hook)

    namespace_escape = json.loads(json.dumps(document))
    namespace_escape["services"]["relay"]["pid"] = "service:worker"
    hostile_documents.append(namespace_escape)

    external_network = json.loads(json.dumps(document))
    external_network["networks"]["egress"]["external"] = True
    hostile_documents.append(external_network)

    named_network = json.loads(json.dumps(document))
    named_network["networks"]["egress"]["name"] = "shared-host-network"
    hostile_documents.append(named_network)

    custom_ipam = json.loads(json.dumps(document))
    custom_ipam["networks"]["egress"]["ipam"] = {"driver": "unreviewed-ipam-driver"}
    hostile_documents.append(custom_ipam)

    named_runner_config = json.loads(json.dumps(document))
    named_runner_config["configs"]["cookie-runner"]["name"] = "shared-config"
    hostile_documents.append(named_runner_config)

    for hostile_document in hostile_documents:
        hostile_result = validate(hostile_document, require_cookie=True)
        assert hostile_result.returncode != 0

    nested_policy_baseline = json.loads(json.dumps(baseline_document))
    nested_policy_document = json.loads(json.dumps(document))
    for candidate in (nested_policy_baseline, nested_policy_document):
        candidate["services"]["egress-proxy"]["volumes"][1]["source"] = (
            "/srv/acceptance/cookies/x/cookies.txt"
        )
    nested_policy = validate(
        nested_policy_document,
        require_cookie=True,
        baseline=nested_policy_baseline,
    )
    assert nested_policy.returncode != 0

    direct_document = json.loads(json.dumps(document))
    direct_worker = direct_document["services"]["worker"]
    direct_worker.pop("entrypoint")
    direct_worker.pop("configs")
    direct_worker["volumes"] = [data_volume, worker_socket_volume]
    direct_document.pop("configs")
    for platform in ("x", "youtube", "bilibili", "douyin", "tiktok", "instagram"):
        target = f"/run/direct-cookies/{platform}.txt"
        direct_worker["command"].extend(
            ["--cookie-source", f"{platform}:acceptance-{platform}={target}"]
        )
        direct_worker["volumes"].append(
            bind(f"/srv/direct-cookies/{platform}.txt", target, read_only=True)
        )
    direct_preflight = validate(direct_document, require_cookie=False)
    assert direct_preflight.returncode != 0
    direct_full = validate(direct_document, require_cookie=True)
    assert direct_full.returncode != 0

    document["services"]["egress-proxy"]["volumes"][1]["source"] = (
        "/srv/acceptance/cookies"
    )
    rejected = validate(document, require_cookie=True)
    assert rejected.returncode != 0


def test_cookie_acceptance_preserves_optional_zero_to_six_product_contract() -> None:
    runner = read(RUNNER)
    checklist = read(CHECKLIST)

    assert "assert len(cookie_specs) <= 6" in runner
    assert "assert wrapper_mode" in runner
    assert "reviewed_wrapper_required" in runner
    assert "zero to six optional platform sources" in checklist
    assert "six non-secret synthetic sources" in checklist
    assert "Normal deployments may configure zero to six sources" in checklist


def test_runner_has_no_path_derived_or_broad_cleanup_primitives() -> None:
    runner = read(RUNNER)

    forbidden = (
        r"(?m)^\s*eval\b",
        r"""(?m)^\s*(?:source|\.)\s+(?=["'$])""",
        r"(?m)^\s*rm\s+-[^\n]*r",
        r"(?m)^\s*rm\s+--recursive\b",
        r"\bunlink\b",
        r"\bfind\b[^\n]*\s-delete\b",
        r"docker\s+(?:system|image|container|volume|network)\s+prune",
        r"docker\s+volume\s+rm",
        r"compose_local\s+down[^\n]*(?:--volumes|-v(?:\s|$))",
        r"set\s+-x",
    )
    for pattern in forbidden:
        assert re.search(pattern, runner) is None, pattern

    assert "trap cleanup_on_exit EXIT" in runner
    assert 'docker stop --time 5 "$LEASE_CONTAINER"' in runner
    assert "compose_local stop -t 30" in runner
    assert "compose_local down --timeout 30" in runner

    first_start = runner.index(
        "if compose_local up -d --no-build --pull never --force-recreate"
    )
    cleanup_armed = runner.rfind("STACK_STARTED=1", 0, first_start)
    assert cleanup_armed != -1
    assert first_start - cleanup_armed < 80


def test_runner_report_is_fixed_field_and_does_not_emit_private_inputs() -> None:
    runner = read(RUNNER)

    report_template = (
        '{\\"schema\\":\\"vdc-linux-docker-acceptance-v1\\",'
        '\\"run_id\\":\\"$RUN_ID\\",\\"timestamp\\":\\"$timestamp\\",'
        '\\"check\\":\\"$check_id\\",\\"status\\":\\"$status\\",'
        '\\"detail\\":\\"$detail\\"}'
    )
    assert report_template in runner
    assert "fixed_fields_only_no_command_output" in runner

    sensitive_variables = {
        "$ENV_FILE",
        "$COMPOSE_OVERRIDE",
        "$TOOL_IMAGE",
        "$BACKUP_ROOT",
        "$RESTORE_PARENT",
        "$DATA_SOURCE",
        "$SOCKET_SOURCE",
        "$POLICY_SOURCE",
        "$FROZEN_COMPOSE_FILE",
    }
    reporting_lines = [
        line
        for line in runner.splitlines()
        if re.match(
            r"^\s*(?:emit|pass_check|fail_check|block_check|fatal_check)\b",
            line,
        )
    ]
    assert reporting_lines
    assert not any(
        variable in line for line in reporting_lines for variable in sensitive_variables
    )
    assert "docker compose logs" not in runner
    assert "docker logs" not in runner


def test_checklist_states_nonclaims_and_retained_cleanup_boundary() -> None:
    checklist = read(CHECKLIST)
    normalized_checklist = " ".join(checklist.split())

    for boundary in (
        "has **not** been executed",
        "does not support exact attachment selection",
        "must not create graph Jobs",
        "does not claim a real DNS-rebinding",
        "does not claim an in-flight real downloader subprocess",
        "Do not infer or write any of these conclusions",
        "real yt-dlp, FFmpeg, ffprobe, Cookie authentication",
        "Stage 0",
        "stale Unix socket for explicit cleanup",
        "contains no recursive delete",
        "Schema 11 restore",
        "A Schema 8, 9, or 10 backup must first",
    ):
        assert boundary in normalized_checklist


@pytest.mark.skipif(
    BASH is None,
    reason="Bash syntax check requires Bash",
)
def test_linux_acceptance_runner_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        [BASH, "-n", str(RUNNER)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    BASH is None,
    reason="Runner help smoke check requires Bash",
)
def test_linux_acceptance_runner_help_is_non_mutating() -> None:
    completed = subprocess.run(
        [BASH, "-p", str(RUNNER), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "Default preflight performs no build" in completed.stdout
    assert "ABSOLUTE_SCHEMA11_BACKUP_DIR" in completed.stdout
    assert "ABSOLUTE_SCHEMA9_BACKUP_DIR" not in completed.stdout
    assert completed.stderr == ""


@pytest.mark.skipif(
    BASH is None,
    reason="Runner launcher check requires Bash",
)
def test_linux_acceptance_runner_rejects_non_privileged_bash_launch() -> None:
    completed = subprocess.run(
        [BASH, str(RUNNER), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "requires a direct shebang launch or /bin/bash -p" in completed.stderr


@pytest.mark.skipif(
    BASH is None,
    reason="Runner preflight smoke check requires Bash",
)
def test_default_preflight_emits_only_sanitized_jsonl() -> None:
    environment = os.environ.copy()
    for name in (
        "VDC_CANDIDATE_IMAGE",
        "VDC_ENABLE_CANDIDATE_REAL_WORKER",
        "VDC_ENABLE_X_GRAPH_V2",
        "VDC_DATA_ROOT",
        "VDC_SOCKET_ROOT",
        "VDC_EGRESS_POLICY_FILE",
        "VDC_COOKIE_SOURCE_ROOT",
        "VDC_COOKIE_MAPPING_FILE",
        "VDC_YT_DLP_VERSION",
        "VDC_FFMPEG_VERSION",
        "VDC_FFPROBE_VERSION",
        "VDC_EGRESS_POLICY_VERSION",
    ):
        environment.pop(name, None)
    completed = subprocess.run(
        [BASH, "-p", str(RUNNER), "--preflight"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode in {0, 1, 2}
    assert completed.stderr == ""
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert records
    expected_keys = {"schema", "run_id", "timestamp", "check", "status", "detail"}
    assert all(set(record) == expected_keys for record in records)
    assert all(
        record["schema"] == "vdc-linux-docker-acceptance-v1" for record in records
    )
    assert records[0]["check"] == "meta"
    assert records[-1]["check"] == "summary"
    expected_exit = {"pass": 0, "fail": 1, "blocked": 2}
    assert completed.returncode == expected_exit[records[-1]["status"]]
    assert str(ROOT) not in completed.stdout
    assert "sha256:" not in completed.stdout
    assert "http://" not in completed.stdout
    assert "https://" not in completed.stdout
