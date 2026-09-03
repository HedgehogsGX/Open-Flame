from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deployment"
COOKIE_PLATFORMS = {
    "x": ("X", "/run/vdc-cookie-sources/x/cookies.txt"),
    "youtube": ("YOUTUBE", "/run/vdc-cookie-sources/youtube/cookies.txt"),
    "bilibili": ("BILIBILI", "/run/vdc-cookie-sources/bilibili/cookies.txt"),
    "douyin": ("DOUYIN", "/run/vdc-cookie-sources/douyin/cookies.txt"),
    "tiktok": ("TIKTOK", "/run/vdc-cookie-sources/tiktok/cookies.txt"),
    "instagram": ("INSTAGRAM", "/run/vdc-cookie-sources/instagram/cookies.txt"),
}


def read(name: str) -> str:
    return (DEPLOYMENT / name).read_text(encoding="utf-8")


def parse_hash_lock(payload: str) -> dict[str, tuple[str, set[str]]]:
    parsed: dict[str, tuple[str, set[str]]] = {}
    current_name: str | None = None
    for line in payload.splitlines():
        requirement = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+-]*) \\",
            line,
        )
        if requirement:
            current_name = requirement.group(1).lower().replace("_", "-")
            assert current_name not in parsed
            parsed[current_name] = (requirement.group(2), set())
            continue
        digest = re.search(r"--hash=(sha256:[0-9a-f]{64})", line)
        if digest:
            assert current_name is not None
            parsed[current_name][1].add(digest.group(1))
    return parsed


def test_candidate_dockerfile_pins_base_and_requires_digest_tool_bundle() -> None:
    dockerfile = read("Dockerfile.candidate")

    assert (
        "python:3.12.13-slim-bookworm@sha256:"
        "4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
    ) in dockerfile
    pinned_python = (
        "FROM python:3.12.13-slim-bookworm@sha256:"
        "4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
    )
    assert dockerfile.count(pinned_python) == 2
    assert "ARG VDC_PYTHON_IMAGE" not in dockerfile
    assert "@sha256:[0-9a-f]{64}" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "video_download_control-0.10.0-py3-none-any.whl" in dockerfile
    assert not dockerfile.startswith("# syntax=")
    assert "requirements.build.lock" in dockerfile
    assert "requirements.runtime.lock" in dockerfile
    assert "pip download" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert dockerfile.count("--no-deps") >= 4
    assert "python -m pip check" in dockerfile
    assert dockerfile.count("python -m pip ") == 6
    assert (
        "COPY --from=wheel_builder "
        "/project-wheel/video_download_control-0.10.0-py3-none-any.whl "
        "/tmp/video_download_control-0.10.0-py3-none-any.whl"
    ) in dockerfile

    download_start = dockerfile.index("RUN python -m pip download")
    build_install_start = dockerfile.index(
        "RUN --network=none python -m pip install", download_start
    )
    download_step = dockerfile[download_start:build_install_start]
    for flag in ("--no-deps", "--only-binary=:all:", "--require-hashes"):
        assert flag in download_step
    assert "--no-index" not in download_step

    source_copy = dockerfile.index("COPY pyproject.toml", build_install_start)
    build_install_step = dockerfile[build_install_start:source_copy]
    for flag in (
        "--network=none",
        "--no-deps",
        "--no-index",
        "--only-binary=:all:",
        "--require-hashes",
    ):
        assert flag in build_install_step

    project_build_start = dockerfile.index(
        "RUN --network=none python -m pip wheel", source_copy
    )
    runtime_stage = dockerfile.index(pinned_python + " AS runtime")
    project_build_step = dockerfile[project_build_start:runtime_stage]
    for flag in (
        "--network=none",
        "--no-deps",
        "--no-index",
        "--no-build-isolation",
    ):
        assert flag in project_build_step

    runtime_install_start = dockerfile.index(
        "RUN --network=none python -m pip install", runtime_stage
    )
    runtime_install_step = dockerfile[runtime_install_start:]
    dependency_install, project_install = runtime_install_step.split(
        "&& python -m pip install", maxsplit=1
    )
    for flag in (
        "--network=none",
        "--no-deps",
        "--no-index",
        "--only-binary=:all:",
        "--require-hashes",
    ):
        assert flag in dependency_install
    assert "--no-deps" in project_install
    assert "--no-index" in project_install
    assert "/tmp/video_download_control-0.10.0-py3-none-any.whl" in project_install
    for required in ("yt-dlp", "ffmpeg", "ffprobe", "bundle-manifest.json"):
        assert f"/opt/vdc-tools/{required}" in dockerfile
    assert ":latest" not in dockerfile
    run_starts = [line for line in dockerfile.splitlines() if line.startswith("RUN ")]
    assert run_starts
    assert run_starts[0].startswith("RUN python -m pip download")
    assert all(line.startswith("RUN --network=none ") for line in run_starts[1:])


def test_python_build_and_runtime_lock_files_are_exact_and_hashed() -> None:
    build_input = read("requirements.build.in")
    build_lock = read("requirements.build.lock")
    runtime_lock = read("requirements.runtime.lock")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert build_input == "hatchling==1.27.0\n"
    assert 'requires = ["hatchling==1.27.0"]' in pyproject

    parsed_locks = [parse_hash_lock(lock) for lock in (build_lock, runtime_lock)]
    for lock, parsed in zip((build_lock, runtime_lock), parsed_locks, strict=True):
        assert " @ " not in lock
        assert "--index-url" not in lock
        assert parsed
        assert all(hashes for _, hashes in parsed.values())

    assert "hatchling==1.27.0 \\" in build_lock
    assert set(parsed_locks[0]) == {
        "hatchling",
        "packaging",
        "pathspec",
        "pluggy",
        "trove-classifiers",
    }
    for direct_runtime in ("fastapi==0.141.1 \\", "uvicorn==0.52.4 \\"):
        assert direct_runtime in runtime_lock

    uv_document = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    uv_packages = {
        package["name"]: package
        for package in uv_document["package"]
        if package["name"] != "video-download-control"
    }
    root = next(
        package
        for package in uv_document["package"]
        if package["name"] == "video-download-control"
    )
    pending = [dependency["name"] for dependency in root["dependencies"]]
    expected_names: set[str] = set()
    while pending:
        name = pending.pop()
        if name in expected_names:
            continue
        expected_names.add(name)
        pending.extend(
            dependency["name"]
            for dependency in uv_packages[name].get("dependencies", [])
        )

    runtime_packages = parsed_locks[1]
    assert set(runtime_packages) == expected_names
    for name in expected_names:
        uv_package = uv_packages[name]
        locked_version, locked_hashes = runtime_packages[name]
        assert locked_version == uv_package["version"]
        expected_hashes = {wheel["hash"] for wheel in uv_package.get("wheels", [])}
        if "sdist" in uv_package:
            expected_hashes.add(uv_package["sdist"]["hash"])
        assert locked_hashes == expected_hashes

    runner = read("run-linux-acceptance.sh")
    contract_start = runner.index("expected = {")
    contract_end = runner.index("raise SystemExit(", contract_start)
    runtime_contract = runner[contract_start:contract_end]
    for name, (locked_version, _) in runtime_packages.items():
        assert f'"{name}": "{locked_version}"' in runtime_contract
    for build_only in parsed_locks[0]:
        assert f'"{build_only}"' in runtime_contract


def test_compose_keeps_worker_and_relay_in_none_namespace() -> None:
    compose = read("compose.candidate.yaml")

    assert "network_mode: none" in compose
    assert compose.count('network_mode: "service:sandbox"') == 2
    assert "VDC_ENABLE_CANDIDATE_REAL_WORKER" in compose
    assert "--poll-interval-seconds" in compose
    assert "--allowed-host-file" in compose
    assert "create_host_path: false" in compose
    assert "ports:" not in compose
    assert "network_mode: host" not in compose
    assert "build:" not in compose
    assert "pull_policy: never" in compose


def test_compose_applies_common_nonroot_and_resource_boundaries() -> None:
    compose = read("compose.candidate.yaml")

    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert 'cap_drop: ["ALL"]' in compose
    assert 'security_opt: ["no-new-privileges:true"]' in compose
    assert compose.count("pids_limit:") == 4
    assert compose.count("mem_limit:") == 4
    assert compose.count("cpus:") == 4
    assert compose.count("healthcheck:") == 4
    assert compose.count('profiles: ["candidate-real-worker"]') == 1


def test_checked_in_candidate_values_are_fail_closed() -> None:
    environment = read(".env.candidate.example")
    hosts = read("egress-hosts.candidate.txt")

    assert "VDC_ENABLE_CANDIDATE_REAL_WORKER=0" in environment
    assert "replace.invalid" in environment
    assert re.search(r"@sha256:0{64}$", environment, re.MULTILINE)
    active_hosts = [
        line.strip()
        for line in hosts.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert active_hosts == ["replace.invalid"]


def test_compose_mounts_optional_cookie_config_read_only_only_in_worker() -> None:
    base_compose = read("compose.candidate.yaml")
    cookie_compose = read("compose.candidate.cookies.yaml")
    worker_start = cookie_compose.index("  worker:")
    worker_end = cookie_compose.index("\nconfigs:", worker_start)
    worker = cookie_compose[worker_start:worker_end]
    non_worker = cookie_compose[:worker_start] + cookie_compose[worker_end:]

    for cookie_token in (
        "VDC_COOKIE_",
        "/run/vdc-cookie",
        "cookie-runner",
        "--cookie-source",
        "OPAQUE_REF",
    ):
        assert cookie_token not in base_compose
    assert "--cookie-source" not in cookie_compose
    assert "OPAQUE_REF" not in cookie_compose
    assert "- /run/vdc-cookie-runner.sh" in worker
    assert "source: cookie-runner" in worker
    assert "file: ./cookies/run-candidate-worker.sh" in cookie_compose
    for source_variable, target in (
        ("VDC_COOKIE_SOURCE_ROOT", "/run/vdc-cookie-sources"),
        ("VDC_COOKIE_MAPPING_FILE", "/run/vdc-cookie-mapping.env"),
    ):
        assert f'source: "${{{source_variable}:?' in worker
        assert f"target: {target}" in worker
        assert source_variable not in non_worker
        assert target not in non_worker
        mount_start = worker.index(f'source: "${{{source_variable}:?')
        mount = worker[mount_start : mount_start + 360]
        assert "read_only: true" in mount
        assert "create_host_path: false" in mount


def test_cookie_mapping_template_has_only_fail_closed_deployment_metadata() -> None:
    mapping = read("cookies/cookie-sources.env.example")
    base_environment = read(".env.candidate.example")
    entries = dict(
        line.split("=", 1)
        for line in mapping.splitlines()
        if line and not line.startswith("#")
    )
    expected_keys: set[str] = set()

    for variable_name, _ in COOKIE_PLATFORMS.values():
        reference_key = f"VDC_COOKIE_{variable_name}_OPAQUE_REF"
        expected_keys.add(reference_key)
        assert entries[reference_key] == ""

    assert set(entries) == expected_keys
    assert not any(
        re.search(rf"^{re.escape(key)}=", base_environment, re.MULTILINE)
        for key in expected_keys
    )
    assert "# VDC_COOKIE_SOURCE_ROOT=/srv/vdc/cookies" in base_environment
    assert (
        "# VDC_COOKIE_MAPPING_FILE=/srv/vdc/config/cookie-sources.env"
        in base_environment
    )
    assert "# Netscape HTTP Cookie File" not in mapping
    assert "\tTRUE\t" not in mapping
    cookie_directory_entries = {
        path.name for path in (DEPLOYMENT / "cookies").iterdir()
    }
    assert cookie_directory_entries == {
        ".gitignore",
        "cookie-sources.env.example",
        "run-candidate-worker.sh",
        "validate-cookie-sources.sh",
    }


def test_cookie_runner_adds_only_nonempty_fixed_platform_mappings() -> None:
    runner = read("cookies/run-candidate-worker.sh")

    assert "MAPPING_FILE=/run/vdc-cookie-mapping.env" in runner
    assert "SOURCE_ROOT=/run/vdc-cookie-sources" in runner
    assert "export LC_ALL=C" in runner
    assert "set +x" in runner
    assert "unset ENV POSIXLY_CORRECT" in runner
    assert 'exec 3< "$MAPPING_FILE"' in runner
    assert "done <&3" in runner
    assert 'exec "$@"' in runner
    assert ". $MAPPING_FILE" not in runner
    assert "source $MAPPING_FILE" not in runner
    for platform, (variable_name, _) in COOKIE_PLATFORMS.items():
        reference = f"{platform}_ref"
        source_variable = f"{platform}_source"
        assert f"VDC_COOKIE_{variable_name}_OPAQUE_REF)" in runner
        assert f"{source_variable}=$SOURCE_ROOT/{platform}/cookies.txt" in runner
        assert f'[ -z "${reference}" ] || set -- "$@" --cookie-source' in runner
        assert f'"{platform}:${reference}=${source_variable}"' in runner
    assert "unmapped source is present" in runner
    assert "opaque reference is reused" in runner
    assert "set -x" not in runner
    assert not any(
        "$source_path" in line or "$reference" in line
        for line in runner.splitlines()
        if "printf" in line
    )


def test_cookie_metadata_preflight_checks_ownership_without_reading_content() -> None:
    validator = read("cookies/validate-cookie-sources.sh")

    assert validator.startswith("#!/bin/sh\n")
    assert "set +x" in validator
    assert "unset ENV POSIXLY_CORRECT" in validator
    for contract in (
        "EXPECTED_UID=0",
        "EXPECTED_GID=10001",
        "EXPECTED_ROOT_UID=0",
        "EXPECTED_ROOT_GID=10001",
        "EXPECTED_PARENT_UID=0",
        "EXPECTED_PARENT_GID=0",
        "EXPECTED_PARENT_MODE=755",
        "EXPECTED_DIRECTORY_MODE=750",
        "EXPECTED_FILE_MODE=440",
        "MAX_COOKIE_BYTES=16777216",
        "MAX_MAPPING_BYTES=4096",
        "require_tool find",
        "require_tool getfacl",
        ': "${VDC_DATA_ROOT:?set the application data root}"',
        ': "${VDC_SOCKET_ROOT:?set the Unix socket root}"',
        "validate_root_owned_ancestor_chain root-ancestor",
        "validate_root_owned_ancestor_chain data-ancestor",
        "validate_root_owned_ancestor_chain socket-ancestor",
        "validate_root_owned_ancestor_chain mapping-ancestor",
        "extended or default ACL is not allowed",
        'paths_overlap "$VDC_COOKIE_SOURCE_ROOT" "$protected_root"',
        'paths_overlap "$VDC_COOKIE_MAPPING_FILE" "$protected_root"',
        '"$VDC_DATA_ROOT" "$VDC_SOCKET_ROOT" "$repository_root"',
        "Cookie deployment path overlaps a protected root",
        "realpath -e",
        "stat -Lc",
        "file has multiple hard links",
        "source is shared across platforms",
        "opaque reference is reused",
        "mapping changed during validation",
        "source changed during validation",
        "revalidate_source_snapshot x",
        "mapping must stay outside source root",
        "path is not canonical",
        "path is a link",
        "cookies.txt.next",
        "VDC_COOKIE_STAGING_PLATFORM",
        'if [ "$VDC_COOKIE_STAGING_PLATFORM" = "$platform" ] &&',
        'fail "$platform" "staging requires a mapped platform"',
        '[ -z "$reference" ] && [ ! -e "$platform_directory" ]',
    ):
        assert contract in validator
    for content_reader in ("cat", "head", "tail", "dd", "od", "xxd", "sha256sum"):
        assert re.search(rf"(^|[ ;|]){content_reader}([ ;|]|$)", validator) is None
    assert "set -x" not in validator
    assert not any(
        "$source_path" in line or "$reference" in line
        for line in validator.splitlines()
        if "printf" in line
    )


def test_deployment_docs_require_stop_atomic_replace_recreate_rotation() -> None:
    documentation = read("README.md")

    stop_position = documentation.index("Stop `worker`")
    staging_position = documentation.index("VDC_COOKIE_STAGING_PLATFORM", stop_position)
    replace_position = documentation.index("mv -fT", staging_position)
    recreate_position = documentation.index("--force-recreate worker", replace_position)
    assert stop_position < staging_position < replace_position < recreate_position
    promotion = documentation[documentation.index("COOKIE_PLATFORM=youtube") :]
    clean_launcher = promotion.index("if ! /usr/bin/env -i")
    staging_gate = promotion.index(
        'VDC_COOKIE_STAGING_PLATFORM="$COOKIE_PLATFORM"', clean_launcher
    )
    next_assignment = promotion.index(
        'NEXT="$COOKIE_ROOT/$COOKIE_PLATFORM/cookies.txt.next"'
    )
    live_assignment = promotion.index(
        'LIVE="$COOKIE_ROOT/$COOKIE_PLATFORM/cookies.txt"'
    )
    atomic_replace = promotion.index('mv -fT -- "$NEXT" "$LIVE" 2>/dev/null')
    assert clean_launcher < staging_gate < next_assignment < live_assignment
    assert live_assignment < atomic_replace
    assert "\n  config\n" not in documentation
    for boundary in (
        "credential-free mode and has no Cookie path",
        "compose.candidate.cookies.yaml",
        "never opens, hashes or prints a Cookie file",
        "Only `worker` has the root, mapping and runner",
        "sh deployment/cookies/validate-cookie-sources.sh",
        "config --quiet",
        "cannot render an opaque ref",
        "exclusive privileged-writer lock",
        "same validated directory",
        "outside `VDC_DATA_ROOT`",
        "must not enter a backup control manifest",
        "no real source was created or mounted",
    ):
        assert boundary in documentation


def test_worker_disables_core_dumps_and_build_context_excludes_private_inputs() -> None:
    compose = read("compose.candidate.yaml")
    worker = compose[compose.index("  worker:\n") : compose.index("\nnetworks:\n")]
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "core: {soft: 0, hard: 0}" in worker
    for private_pattern in (
        "**/cookies.txt",
        "**/cookies.txt.next",
        "**/cookie-sources.env",
        "**/candidate.env",
    ):
        assert private_pattern in dockerignore


def test_cookie_deployment_paths_do_not_enter_control_plane_modules() -> None:
    forbidden = {
        "/run/vdc-cookie-sources",
        "/run/vdc-cookie-mapping.env",
        "/srv/vdc/cookies",
        "VDC_COOKIE_SOURCE_ROOT",
        "VDC_COOKIE_MAPPING_FILE",
    }
    control_plane_files = (
        "src/video_download_control/api.py",
        "src/video_download_control/schemas.py",
        "src/video_download_control/database.py",
        "src/video_download_control/backup.py",
        "src/video_download_control/diagnostics.py",
        "src/video_download_control/observability.py",
    )

    for relative_path in control_plane_files:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert not any(value in content for value in forbidden)


@pytest.mark.parametrize("source_count", [0, 1, 3, 6])
@pytest.mark.skipif(
    os.name != "posix"
    or not hasattr(os, "geteuid")
    or os.geteuid() != 0
    or shutil.which("getfacl") is None
    or not Path("/srv").is_dir(),
    reason="real metadata contract requires a root POSIX host with getfacl",
)
def test_cookie_validator_accepts_zero_to_six_synthetic_sources(
    source_count: int,
) -> None:
    test_root = Path(tempfile.mkdtemp(prefix="vdc-cookie-contract-", dir="/srv"))
    source_root = test_root / "cookies"
    mapping_root = test_root / "config"
    mapping_file = mapping_root / "cookie-sources.env"
    data_root = test_root / "data"
    socket_root = test_root / "socket"
    platforms = tuple(COOKIE_PLATFORMS)
    try:
        test_root.chmod(0o755)
        for path, uid, gid, mode in (
            (source_root, 0, 10001, 0o750),
            (mapping_root, 0, 10001, 0o750),
            (data_root, 10001, 10001, 0o750),
            (socket_root, 10001, 10001, 0o770),
        ):
            path.mkdir()
            os.chown(path, uid, gid)
            path.chmod(mode)

        mapping_lines: list[str] = []
        for index, platform in enumerate(platforms):
            variable = COOKIE_PLATFORMS[platform][0]
            reference = f"acceptance-{platform}" if index < source_count else ""
            mapping_lines.append(f"VDC_COOKIE_{variable}_OPAQUE_REF={reference}")
            if not reference:
                continue
            platform_root = source_root / platform
            platform_root.mkdir()
            os.chown(platform_root, 0, 10001)
            platform_root.chmod(0o750)
            cookie_file = platform_root / "cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n.synthetic.invalid\tTRUE\t/\tFALSE\t0\tname\tvalue\n",
                encoding="utf-8",
            )
            os.chown(cookie_file, 0, 10001)
            cookie_file.chmod(0o440)

        mapping_file.write_text("\n".join(mapping_lines) + "\n", encoding="utf-8")
        os.chown(mapping_file, 0, 10001)
        mapping_file.chmod(0o440)
        validator = DEPLOYMENT / "cookies" / "validate-cookie-sources.sh"
        completed = subprocess.run(
            [
                "/usr/bin/env",
                "-i",
                f"VDC_COOKIE_SOURCE_ROOT={source_root}",
                f"VDC_COOKIE_MAPPING_FILE={mapping_file}",
                f"VDC_DATA_ROOT={data_root}",
                f"VDC_SOCKET_ROOT={socket_root}",
                "VDC_COOKIE_STAGING_PLATFORM=",
                "/bin/sh",
                str(validator),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert "configured sources remain optional" in completed.stdout
    finally:
        shutil.rmtree(test_root)
