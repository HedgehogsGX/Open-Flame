from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import video_download_control.build_identity as build_identity_module
from video_download_control import __version__
from video_download_control.build_identity import (
    ProductBuildDriftError,
    current_product_identity,
    package_payload_sha256,
    product_build_sha256,
)


def test_current_product_identity_binds_version_to_full_package_digest() -> None:
    digest = product_build_sha256()

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert current_product_identity() == f"{__version__}+build.sha256.{digest}"


def test_package_payload_digest_covers_data_and_ignores_generated_bytecode(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    module = package / "module.py"
    data = package / "config.json"
    cache = package / "__pycache__"
    cache.mkdir()
    bytecode = cache / "module.pyc"
    module.write_bytes(b"VALUE = 1\n")
    data.write_bytes(b'{"policy": 1}\n')
    bytecode.write_bytes(b"generated-one")

    original = package_payload_sha256(package)
    bytecode.write_bytes(b"generated-two")
    assert package_payload_sha256(package) == original

    data.write_bytes(b'{"policy": 2}\n')
    assert package_payload_sha256(package) != original


def test_package_payload_digest_is_path_independent_and_layout_sensitive(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "package"
    second = tmp_path / "second" / "package"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for root in (first, second):
        (root / "module.py").write_bytes(b"VALUE = 1\n")
        (root / "policy.json").write_bytes(b'{"enabled": true}\n')

    baseline = package_payload_sha256(first)
    assert package_payload_sha256(second) == baseline

    (second / "module.py").rename(second / "renamed.py")
    assert package_payload_sha256(second) != baseline


def test_current_product_identity_fails_when_payload_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_identity_module,
        "product_build_sha256",
        lambda: "f" * 64,
    )

    with pytest.raises(ProductBuildDriftError):
        current_product_identity()


@pytest.mark.parametrize("target_location", ("inside", "outside"))
def test_package_payload_rejects_linked_directories(
    tmp_path: Path,
    target_location: str,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_bytes(b"VALUE = 1\n")
    target = (
        package / "target"
        if target_location == "inside"
        else tmp_path / "outside"
    )
    target.mkdir()
    (target / "linked_module.py").write_bytes(b"VALUE = 2\n")
    link = package / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {type(exc).__name__}")

    with pytest.raises(RuntimeError, match="must not contain links"):
        package_payload_sha256(package)


def test_package_payload_digest_covers_sourceless_bytecode(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    bytecode = package / "payload.pyc"
    bytecode.write_bytes(b"sourceless-one")

    original = package_payload_sha256(package)
    bytecode.write_bytes(b"sourceless-two")

    assert package_payload_sha256(package) != original


def test_package_payload_digest_covers_non_bytecode_inside_cache(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    (package / "module.py").write_bytes(b"VALUE = 1\n")
    policy = cache / "policy.json"
    policy.write_bytes(b'{"policy": "one"}\n')

    original = package_payload_sha256(package)
    policy.write_bytes(b'{"policy": "two"}\n')

    assert package_payload_sha256(package) != original


def test_package_payload_rejects_links_inside_cache(tmp_path: Path) -> None:
    package = tmp_path / "package"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    module = package / "module.py"
    module.write_bytes(b"VALUE = 1\n")
    link = cache / "linked.pyc"
    try:
        link.symlink_to(module)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {type(exc).__name__}")

    with pytest.raises(RuntimeError, match="must not contain links"):
        package_payload_sha256(package)


def test_package_payload_rejects_file_added_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    module = package / "module.py"
    module.write_bytes(b"VALUE = 1\n")
    original_open = Path.open
    injected = False

    def open_with_late_file(path: Path, *args, **kwargs):
        nonlocal injected
        if path == module and not injected:
            injected = True
            (package / "late.py").write_bytes(b"LATE = True\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_late_file)

    with pytest.raises(RuntimeError, match="changed while hashing"):
        package_payload_sha256(package)


def test_validation_cli_sanitizes_build_hash_failure_during_import() -> None:
    project_root = Path(__file__).resolve().parents[1]
    private_canary = r"C:\\Users\\PRIVATE-CANARY\\package"
    script = f"""
import os
def fail_scandir(path):
    raise OSError({private_canary!r})
os.scandir = fail_scandir
from video_download_control.validation_cli import main
raise SystemExit(main(["--print-product-identity"]))
"""
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH")
    source_root = str(project_root / "src")
    environment["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else source_root + os.pathsep + existing_pythonpath
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 6
    assert completed.stdout == ""
    assert json.loads(completed.stderr) == {
        "status": "error",
        "error_code": "product_build_unavailable",
    }
    assert "PRIVATE-CANARY" not in completed.stderr
    assert "Traceback" not in completed.stderr
