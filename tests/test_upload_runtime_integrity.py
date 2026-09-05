"""Synthetic integrity boundary for the separately installed upload runtime."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from video_download_control.uploads import runtime_setup as setup
from video_download_control.uploads.backend import SauBackend


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path, *, exclude_manifest: bool = False) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        if exclude_manifest and relative == "manifest.json":
            continue
        result[relative] = digest(path)
    return result


def synthetic_runtime(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    root = tmp_path / "upload-root"
    runtime = root / "runtime"
    source = runtime / "source"
    archives = runtime / "archives"
    wheels = runtime / "wheels"
    site = runtime / "venv" / "Lib" / "site-packages"
    python = setup.runtime_python(root)
    base = tmp_path / "cpython"
    for directory in (source, archives, wheels, site, python.parent, base / "Lib", runtime / "chrome"):
        directory.mkdir(parents=True, exist_ok=True)

    source_payload = b"# pinned synthetic uploader\n"
    (source / "sau_cli.py").write_bytes(source_payload)
    source_archive = archives / f"social-auto-upload-{setup.SAU_COMMIT}.zip"
    with zipfile.ZipFile(source_archive, "w") as bundle:
        bundle.writestr("repository/sau_cli.py", source_payload)
    monkeypatch.setattr(setup, "SAU_SHA256", digest(source_archive))

    biliup_payload = b"synthetic biliup executable"
    (runtime / "biliup.exe").write_bytes(biliup_payload)
    biliup_archive = archives / f"biliup-{setup.BILIUP_VERSION}-windows-x64.zip"
    with zipfile.ZipFile(biliup_archive, "w") as bundle:
        bundle.writestr("biliup.exe", biliup_payload)
    monkeypatch.setattr(setup, "BILIUP_SHA256", digest(biliup_archive))

    metadata = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
    wheel = wheels / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as bundle:
        bundle.writestr("demo.py", b"VALUE = 1\n")
        bundle.writestr("demo-1.0.dist-info/METADATA", metadata)
        bundle.writestr("demo-1.0.data/headers/demo.h", b"/* demo header */\n")
    lock = tmp_path / "runtime-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema": 1,
                "retrieved": "2026-09-05",
                "packages": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "sha256": [digest(wheel)],
                        "source": "https://pypi.org/pypi/demo/1.0/json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "LOCK_PATH", lock)

    (site / "demo.py").write_bytes(b"VALUE = 1\n")
    installed_metadata = site / "demo-1.0.dist-info" / "METADATA"
    installed_metadata.parent.mkdir()
    installed_metadata.write_bytes(metadata)
    installed_header = (
        runtime / "venv" / "Include" / "site" / "python3.12" / "demo" / "demo.h"
    )
    installed_header.parent.mkdir(parents=True)
    installed_header.write_bytes(b"/* demo header */\n")
    python.write_bytes(b"synthetic venv python")
    (runtime / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\nversion = 3.12.0\n",
        encoding="utf-8",
    )
    (runtime / "requirements.lock").write_text(
        f"demo==1.0 --hash=sha256:{digest(wheel)}\n", encoding="utf-8"
    )
    (runtime / "source_revision.txt").write_text(setup.SAU_COMMIT, encoding="ascii")
    (runtime / "chrome" / "chrome.exe").write_bytes(b"synthetic chromium")
    (runtime / "chrome" / "145.0.7632.6.manifest").write_bytes(b"synthetic manifest")

    base_executable = base / "python.exe"
    base_executable.write_bytes(b"synthetic base python")
    (base / "python312.dll").write_bytes(b"synthetic python dll")
    (base / "Lib" / "os.py").write_bytes(b"# synthetic stdlib\n")

    manifest = {
        "schema": 2,
        "sau_commit": setup.SAU_COMMIT,
        "sau_archive_sha256": setup.SAU_SHA256,
        "biliup_version": setup.BILIUP_VERSION,
        "biliup_archive_sha256": setup.BILIUP_SHA256,
        "requirements_sha256": digest(lock),
        "chromium_revision": "1208",
        "cli_help_verified": True,
        "browser_launch_verified": True,
        "python": {
            "implementation": "cpython",
            "version": [3, 12, 0],
            "bits": 64,
            "base_prefix": str(base.absolute()),
            "base_executable": str(base_executable.absolute()),
        },
        "runtime_artifacts": inventory(runtime, exclude_manifest=True),
        "python_artifacts": inventory(base),
    }
    (runtime / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return root, source


def test_runtime_rejects_unlisted_source_that_can_shadow_locked_dependency(
    tmp_path, monkeypatch
):
    root, source = synthetic_runtime(tmp_path, monkeypatch)
    assert setup.inspect_runtime(root) == {"ready": True, "code": "ready"}

    shadow = source / "patchright" / "async_api.py"
    shadow.parent.mkdir()
    shadow.write_text("raise RuntimeError('unlisted code loaded')\n", encoding="utf-8")

    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


def test_writable_manifest_cannot_authorize_changed_locked_wheel_payload(
    tmp_path, monkeypatch
):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    runtime = root / "runtime"
    installed = runtime / "venv" / "Lib" / "site-packages" / "demo.py"
    installed.write_text("VALUE = 2\n", encoding="utf-8")
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = installed.relative_to(runtime).as_posix()
    manifest["runtime_artifacts"][relative] = digest(installed)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


def test_writable_manifest_cannot_authorize_extra_importable_venv_code(
    tmp_path, monkeypatch
):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    runtime = root / "runtime"
    injected = runtime / "venv" / "Lib" / "site-packages" / "json.py"
    injected.write_text("raise RuntimeError('unexpected import path')\n", encoding="utf-8")
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_artifacts"][injected.relative_to(runtime).as_posix()] = digest(
        injected
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


@pytest.mark.parametrize(
    "relative",
    [
        "runtime/source/sau_cli.py",
        "runtime/venv/Lib/site-packages/demo.py",
        "runtime/chrome/chrome.exe",
        "cpython/python.exe",
        "cpython/python312.dll",
    ],
)
def test_runtime_rejects_changed_source_dependency_browser_and_interpreter(
    tmp_path, monkeypatch, relative
):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    target = (root if relative.startswith("runtime/") else tmp_path) / relative
    target.write_bytes(target.read_bytes() + b"tampered")

    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


def test_runtime_rejects_missing_payload_and_redirect(tmp_path, monkeypatch):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    runtime = root / "runtime"
    missing = runtime / "chrome" / "145.0.7632.6.manifest"
    missing.unlink()
    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}

    root, source = synthetic_runtime(tmp_path / "redirect", monkeypatch)
    try:
        (source / "redirect.py").symlink_to(source / "sau_cli.py")
    except OSError as exc:
        pytest.skip(f"file symlink unavailable on this Windows host: {exc}")
    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


@pytest.mark.parametrize("mode", ["check", "install"])
def test_cli_rejects_redirected_root_before_creating_lock_or_files(
    tmp_path, monkeypatch, capsys, mode
):
    target = tmp_path / "redirect-target"
    target.mkdir()
    root = tmp_path / "redirected-root"
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable on this host: {exc}")
    monkeypatch.setattr(
        setup,
        "_download",
        lambda *_args, **_kwargs: pytest.fail(
            "redirected roots must stop before downloading"
        ),
    )
    monkeypatch.setattr(
        setup,
        "_command",
        lambda *_args, **_kwargs: pytest.fail(
            "redirected roots must stop before starting a child"
        ),
    )
    arguments = ["--root", str(root.absolute())]
    if mode == "check":
        arguments.append("--check")
    else:
        arguments.extend(["--python", str(Path(sys.executable).absolute())])

    assert setup.main(arguments) == 1

    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "code": "runtime_redirect_rejected",
    }
    assert list(target.iterdir()) == []


def test_cli_check_missing_root_remains_read_only(tmp_path, capsys):
    root = tmp_path / "missing-upload-root"

    assert setup.main(["--root", str(root.absolute()), "--check"]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "ready": False,
        "code": "runtime_missing",
    }
    assert not root.exists()


def test_adjacent_pycache_is_ignored_only_because_execution_uses_fresh_prefix(
    tmp_path, monkeypatch
):
    root, source = synthetic_runtime(tmp_path, monkeypatch)
    bytecode = source / "__pycache__" / "sau_cli.cpython-312.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"not trusted and never consumed")

    assert setup.inspect_runtime(root) == {"ready": True, "code": "ready"}

    unscoped = source / "sau_cli.pyc"
    unscoped.write_bytes(b"sourceless bytecode would be executable")
    assert setup.inspect_runtime(root) == {"ready": False, "code": "runtime_invalid"}


def test_cached_status_reuses_file_and_archive_digests(tmp_path, monkeypatch):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    cache = setup.RuntimeInspectionCache()
    file_hashes = 0
    archive_reads = 0
    original_file_digest = setup.hashlib.file_digest
    original_archive_read = setup.zipfile.ZipFile.read

    def counted_file_digest(*args, **kwargs):
        nonlocal file_hashes
        file_hashes += 1
        return original_file_digest(*args, **kwargs)

    def counted_archive_read(*args, **kwargs):
        nonlocal archive_reads
        archive_reads += 1
        return original_archive_read(*args, **kwargs)

    monkeypatch.setattr(setup.hashlib, "file_digest", counted_file_digest)
    monkeypatch.setattr(setup.zipfile.ZipFile, "read", counted_archive_read)
    first = setup.inspect_runtime(root, cache=cache)
    assert (first["ready"], first["code"], first["integrity_cached"]) == (
        True,
        "ready",
        False,
    )
    first_file_hashes = file_hashes
    first_archive_reads = archive_reads
    assert first_file_hashes > 10
    assert first_archive_reads > 3

    second = setup.inspect_runtime(root, cache=cache)
    assert (second["ready"], second["code"], second["integrity_cached"]) == (
        True,
        "ready",
        True,
    )
    assert 0 <= second["integrity_age_seconds"] <= second["integrity_ttl_seconds"]
    assert file_hashes == first_file_hashes
    assert archive_reads == first_archive_reads


def test_backend_execution_check_bypasses_warm_status_cache(tmp_path, monkeypatch):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    backend = SauBackend(root)
    file_hashes = 0
    original_file_digest = setup.hashlib.file_digest

    def counted_file_digest(*args, **kwargs):
        nonlocal file_hashes
        file_hashes += 1
        return original_file_digest(*args, **kwargs)

    monkeypatch.setattr(setup.hashlib, "file_digest", counted_file_digest)
    first = backend.inspect()
    assert first["ready"] is True
    assert first["integrity_cached"] is False
    cold_hashes = file_hashes
    second = backend.inspect()
    assert second["ready"] is True
    assert second["integrity_cached"] is True
    assert file_hashes == cold_hashes

    before_execution = file_hashes
    assert backend._inspect_for_execution() == {"ready": True, "code": "ready"}
    assert file_hashes > before_execution + 10


@pytest.mark.parametrize("mutation", ["add", "remove", "same_size"])
def test_status_cache_is_bounded_and_detects_tree_drift_after_ttl(
    tmp_path, monkeypatch, mutation
):
    clock = [100.0]
    monkeypatch.setattr(setup.time, "monotonic", lambda: clock[0])
    root, source = synthetic_runtime(tmp_path, monkeypatch)
    cache = setup.RuntimeInspectionCache()
    assert setup.inspect_runtime(root, cache=cache)["integrity_cached"] is False

    target = source / "sau_cli.py"
    if mutation == "add":
        (source / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    elif mutation == "remove":
        target.unlink()
    else:
        original = target.read_bytes()
        target.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    cached = setup.inspect_runtime(root, cache=cache)
    assert cached["ready"] is True
    assert cached["integrity_cached"] is True
    clock[0] += setup._STATUS_CACHE_SECONDS + 0.001
    expired = setup.inspect_runtime(root, cache=cache)
    assert expired["ready"] is False
    assert expired["code"] == "runtime_invalid"
    assert expired["integrity_cached"] is False


def test_manifest_change_invalidates_status_cache_immediately(tmp_path, monkeypatch):
    root, _ = synthetic_runtime(tmp_path, monkeypatch)
    cache = setup.RuntimeInspectionCache()
    assert setup.inspect_runtime(root, cache=cache)["ready"] is True
    manifest_path = root / "runtime" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sau_commit"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    changed = setup.inspect_runtime(root, cache=cache)
    assert changed["ready"] is False
    assert changed["code"] == "runtime_invalid"
    assert changed["integrity_cached"] is False


def test_isolated_runtime_command_does_not_load_adjacent_timestamp_valid_pyc(
    tmp_path,
):
    source = tmp_path / "source"
    source.mkdir()
    module = source / "shadowed.py"
    fixed_time = 1_700_000_000
    module.write_text("VALUE = 'evil'\n", encoding="utf-8")
    os.utime(module, (fixed_time, fixed_time))
    bytecode = Path(py_compile.compile(str(module), doraise=True))
    module.write_text("VALUE = 'safe'\n", encoding="utf-8")
    os.utime(module, (fixed_time, fixed_time))
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "import shadowed\n"
        "print(shadowed.VALUE)\n",
        encoding="utf-8",
    )

    control = subprocess.run(
        [sys.executable, "-I", "-B", str(probe)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert control.stdout.strip() == "evil"
    assert bytecode.is_file()

    protected = subprocess.run(
        setup.isolated_python_command(
            Path(sys.executable), probe, tmp_path / "empty-pycache"
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    assert protected.stdout.strip() == "safe"
    assert not (tmp_path / "empty-pycache").exists()


@pytest.mark.skipif(os.name != "nt", reason="setup children use a Windows Job")
def test_python_identity_is_captured_by_the_owned_child(tmp_path, monkeypatch):
    root = tmp_path / "identity-root"
    (root / "runtime").mkdir(parents=True)
    monkeypatch.setattr(setup, "runtime_python", lambda _root: Path(sys.executable))

    identity = setup._capture_python_identity(root)

    assert identity["implementation"] == "cpython"
    assert identity["version"][:2] == list(sys.version_info[:2])
    assert identity["bits"] == 64
    assert Path(identity["base_executable"]).is_file()
    assert not (root / "runtime" / "python-identity.tmp").exists()
