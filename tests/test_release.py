"""Project-only release contracts using synthetic archives, never build tools."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import warnings
import zipfile

import pytest

from video_download_control.build_identity import package_payload_sha256


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("open_flame_release_tests", ROOT / "scripts/release.py")
assert _SPEC is not None and _SPEC.loader is not None
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


def _fingerprint(payload: bytes) -> dict[str, str | int]:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


@pytest.fixture
def source_files() -> dict[str, bytes]:
    """Only reviewed source/legal paths, not Git or ignored runtime trees."""
    names = {
        "pyproject.toml", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
        "compliance/python-license-files.sha256",
        "deployment/requirements.build.lock", "deployment/requirements.runtime.lock",
        *release.ENTRYPOINTS,
    }
    for prefix in ("src/video_download_control", "licenses/python"):
        for path in (ROOT / prefix).rglob("*"):
            if "__pycache__" in path.parts:
                continue
            assert not path.is_symlink()
            if path.is_file():
                names.add(path.relative_to(ROOT).as_posix())
    files = {name: (ROOT / name).read_bytes() for name in sorted(names)}
    return _listed(files)


def _listed(files: dict[str, bytes]) -> dict[str, bytes]:
    result = dict(files)
    result["release-files.txt"] = ("\n".join(sorted({*result, "release-files.txt"})) + "\n").encode()
    return result


def _source_tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    for name, payload in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


def _metadata(source: dict[str, bytes], project: dict) -> bytes:
    lines = [
        "Metadata-Version: 2.4", f"Name: {project['name']}",
        f"Version: {project['version']}", "License-Expression: Apache-2.0",
        f"Requires-Python: {project['requires-python']}",
    ]
    legal = {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"}
    legal.update(name for name in source if name.startswith("licenses/python/"))
    lines.extend(f"License-File: {name}" for name in sorted(legal))
    lines.extend(f"Requires-Dist: {dependency}" for dependency in project["dependencies"])
    for extra, dependencies in project.get("optional-dependencies", {}).items():
        lines.append(f"Provides-Extra: {extra}")
        lines.extend(f"Requires-Dist: {dependency}; extra == '{extra}'" for dependency in dependencies)
    return ("\n".join(lines) + "\n\nSynthetic project-only release fixture.\n").encode()


def _record(wheel: dict[str, bytes], record_name: str) -> None:
    rows = []
    for name in sorted({*wheel, record_name}):
        if name == record_name:
            rows.append((name, "", ""))
        else:
            digest = base64.urlsafe_b64encode(hashlib.sha256(wheel[name]).digest()).rstrip(b"=").decode()
            rows.append((name, "sha256=" + digest, str(len(wheel[name]))))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    wheel[record_name] = stream.getvalue().encode()


def _wheel(source: dict[str, bytes]) -> tuple[dict[str, bytes], dict, str]:
    project = tomllib.loads(source["pyproject.toml"].decode())["project"]
    prefix = f"video_download_control-{project['version']}.dist-info/"
    wheel = {name.removeprefix("src/"): payload for name, payload in source.items() if name.startswith("src/video_download_control/")}
    for name, payload in source.items():
        if name in {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"} or name.startswith("licenses/python/"):
            wheel[prefix + "licenses/" + name] = payload
    wheel[prefix + "METADATA"] = _metadata(source, project)
    wheel[prefix + "WHEEL"] = b"Wheel-Version: 1.0\nGenerator: synthetic-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    wheel[prefix + "entry_points.txt"] = ("[console_scripts]\n" + "".join(f"{name} = {value}\n" for name, value in project["scripts"].items())).encode()
    _record(wheel, prefix + "RECORD")
    return wheel, project, prefix


def _zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 2, 2, 0, 0, 0))
            # ZipInfo normalizes backslashes on Windows and truncates NUL in
            # its constructor. Preserve deliberately malformed raw test names.
            info.filename = name
            info.orig_filename = name
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED)


def _tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_release(root: Path, source: dict[str, bytes]) -> dict:
    root.mkdir()
    wheel, project, _ = _wheel(source)
    version = project["version"]
    source_name = f"Open-Flame-{version}-source.zip"
    wheel_name = f"video_download_control-{version}-py3-none-any.whl"
    sdist_name = f"video_download_control-{version}.tar.gz"
    _zip(root / source_name, {f"Open-Flame-{version}-source/{name}": payload for name, payload in source.items()})
    _zip(root / wheel_name, wheel)
    sdist = {f"video_download_control-{version}/{name}": payload for name, payload in source.items()}
    sdist[f"video_download_control-{version}/PKG-INFO"] = _metadata(source, project)
    _tar(root / sdist_name, sdist)
    identity = release.source_contract(source)[1]
    manifest = {
        "schema_version": 1, "version": version,
        "scope": "project-only; no third-party runtime binaries",
        "product_identity": identity, "source_zip": source_name,
        "source_files": {name: _fingerprint(payload) for name, payload in source.items()},
        "entrypoint_sha256": {name: hashlib.sha256(source[name]).hexdigest() for name in release.ENTRYPOINTS},
        "artifacts": {name: _fingerprint((root / name).read_bytes()) for name in (source_name, wheel_name, sdist_name)},
        "privacy_check": "synthetic fixture; not a guarantee of absence of secrets",
        "authenticity": "unsigned checksums; compare through a trusted channel",
    }
    _write_manifest(root, manifest)
    return manifest


def _write_manifest(root: Path, manifest: dict) -> None:
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (root / "release-manifest.json").write_bytes(payload)
    sums = "".join(f"{manifest['artifacts'][name]['sha256']}  {name}\n" for name in sorted(manifest["artifacts"]))
    sums += f"{hashlib.sha256(payload).hexdigest()}  release-manifest.json\n"
    (root / "SHA256SUMS").write_bytes(sums.encode())


def _tree_state(root: Path) -> dict[str, tuple[bytes, int]]:
    return {path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def test_verify_complete_project_only_release_is_read_only_without_checkout(
    tmp_path: Path, source_files: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "independent release"
    expected = _write_release(artifacts, source_files)
    before = _tree_state(tmp_path)
    monkeypatch.setattr(release, "ROOT", tmp_path / "no-git-no-checkout")
    assert release.verify_release(artifacts) == expected
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("name", [
    "", "/absolute", "../escape", "a/../b", "a/./b", "a//b", "a/",
    "C:relative", "C:/absolute", "a\\b", "trailing.", "trailing ",
    "CON", "nul.txt", "nested/COM1.py", "LPT9.log",
    "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b",
    "a\x00b", "a\x01b", "a\tb", "a\nb", "a\rb", "a\x1fb", "a" * 241,
])
def test_safe_name_rejects_unsafe_windows_members(name: str) -> None:
    with pytest.raises(release.ReleaseError, match="^unsafe_member$"):
        release.safe_name(name)


def test_inventory_rejects_windows_case_collisions() -> None:
    with pytest.raises(release.ReleaseError, match="^duplicate_inventory$"):
        release.file_list(b"src/Module.py\nsrc/module.py\n")
    assert release.safe_name("docs/中文 名称!.md") == "docs/中文 名称!.md"


@pytest.mark.parametrize("other_name", ["entry.txt", "ENTRY.txt"])
def test_archive_rejects_duplicate_or_windows_colliding_zip_members(
    tmp_path: Path, other_name: str,
) -> None:
    archive_path = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("entry.txt", b"first")
            archive.writestr(other_name, b"second")
    with pytest.raises(release.ReleaseError, match="^duplicate_member$"):
        release.archive_payloads(archive_path)


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "drive:C.txt", "nested\\escape.txt", "visible\x00hidden"])
def test_archive_rejects_traversal_without_extracting(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    _zip(archive_path, {name: b"synthetic"})
    before = _tree_state(tmp_path)
    with pytest.raises(release.ReleaseError, match="^unsafe_member$"):
        release.archive_payloads(archive_path)
    assert _tree_state(tmp_path) == before


@pytest.mark.parametrize("mode,name", [(stat.S_IFLNK, "link"), (stat.S_IFIFO, "pipe"), (stat.S_IFDIR, "folder/")])
def test_archive_rejects_non_regular_zip_members(tmp_path: Path, mode: int, name: str) -> None:
    archive_path = tmp_path / "special.zip"
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (mode | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, b"outside-target")
    with pytest.raises(release.ReleaseError, match="^non_regular_member$"):
        release.archive_payloads(archive_path)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE])
def test_archive_rejects_tar_links_and_special_members(tmp_path: Path, member_type: bytes) -> None:
    archive_path = tmp_path / "special.tar.gz"
    info = tarfile.TarInfo("member")
    info.type = member_type
    info.linkname = "../outside"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.addfile(info)
    with pytest.raises(release.ReleaseError, match="^non_regular_member$"):
        release.archive_payloads(archive_path)


@pytest.mark.parametrize("kind", ["directory", "hardlink"])
def test_archive_input_itself_must_be_single_link_regular_file(tmp_path: Path, kind: str) -> None:
    archive_path = tmp_path / "archive.zip"
    if kind == "directory":
        archive_path.mkdir()
    else:
        _zip(archive_path, {"member": b"synthetic"})
        os.link(archive_path, tmp_path / "second-name.zip")
    with pytest.raises(release.ReleaseError, match="^(?:non_plain_path|linked_path)$"):
        release.archive_payloads(archive_path)


@pytest.mark.parametrize("kind", ["archive-comment", "member-comment", "extra", "tar-owner", "tar-pax"])
def test_archive_rejects_private_container_metadata(tmp_path: Path, kind: str) -> None:
    if kind.startswith("tar-"):
        archive_path = tmp_path / "metadata.tar.gz"
        info = tarfile.TarInfo("member")
        if kind == "tar-owner":
            info.uname = "SYNTHETIC-OWNER-CANARY"
        else:
            info.pax_headers = {"SCHILY.xattr.user.private": "SYNTHETIC-CANARY"}
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.addfile(info, io.BytesIO())
    else:
        archive_path = tmp_path / "metadata.zip"
        info = zipfile.ZipInfo("member")
        if kind == "member-comment":
            info.comment = b"SYNTHETIC-COMMENT-CANARY"
        elif kind == "extra":
            info.extra = b"\xff\xff\x04\x00test"
        with zipfile.ZipFile(archive_path, "w") as archive:
            if kind == "archive-comment":
                archive.comment = b"SYNTHETIC-COMMENT-CANARY"
            archive.writestr(info, b"data")
    with pytest.raises(release.ReleaseError, match="^archive_private_metadata$"):
        release.archive_payloads(archive_path)


@pytest.mark.parametrize("size,accepted", [(32, True), (33, False)])
def test_archive_member_size_limit_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, accepted: bool,
) -> None:
    archive_path = tmp_path / "size.zip"
    _zip(archive_path, {"member": b"x" * size})
    monkeypatch.setattr(release, "MAX_FILE", 32)
    if accepted:
        assert release.archive_payloads(archive_path) == {"member": b"x" * size}
    else:
        with pytest.raises(release.ReleaseError, match="^archive_limit$"):
            release.archive_payloads(archive_path)


@pytest.mark.parametrize("extra,accepted", [(False, True), (True, False)])
def test_archive_expanded_total_limit_is_inclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: bool, accepted: bool,
) -> None:
    files = {"first": b"x" * 512, "second": b"y" * 512}
    if extra:
        files["third"] = b"z"
    archive_path = tmp_path / "total.zip"
    _zip(archive_path, files)
    assert archive_path.stat().st_size < 1024
    monkeypatch.setattr(release, "MAX_FILE", 512)
    monkeypatch.setattr(release, "MAX_TOTAL", 1024)
    if accepted:
        assert release.archive_payloads(archive_path) == files
    else:
        with pytest.raises(release.ReleaseError, match="^archive_limit$"):
            release.archive_payloads(archive_path)


def test_archive_count_and_container_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "limits.zip"
    _zip(archive_path, {"first": b"a", "second": b"b"})
    monkeypatch.setattr(release, "MAX_ENTRIES", 1)
    with pytest.raises(release.ReleaseError, match="^archive_limit$"):
        release.archive_payloads(archive_path)
    monkeypatch.setattr(release, "MAX_ENTRIES", 2)
    monkeypatch.setattr(release, "MAX_TOTAL", archive_path.stat().st_size - 1)
    with pytest.raises(release.ReleaseError, match="^archive_too_large$"):
        release.archive_payloads(archive_path)


def test_source_identity_matches_runtime_package_algorithm(source_files: dict[str, bytes]) -> None:
    project, identity = release.source_contract(source_files)
    expected = package_payload_sha256(ROOT / "src/video_download_control")
    assert identity == f"{project['version']}+build.sha256.{expected}"


@pytest.mark.parametrize("extra_name", ["extra.py", "extra-policy.json"])
def test_source_snapshot_rejects_unlisted_importable_package_payload(
    tmp_path: Path, source_files: dict[str, bytes], extra_name: str,
) -> None:
    checkout = _source_tree(tmp_path / "source", source_files)
    (checkout / "src/video_download_control" / extra_name).write_bytes(b"synthetic extra\n")
    with pytest.raises(release.ReleaseError, match="^unlisted_package_file$"):
        release.source_snapshot(checkout)


def test_source_contract_rejects_payload_not_declared_by_inventory(source_files: dict[str, bytes]) -> None:
    source_files["src/video_download_control/extra.json"] = b"{}\n"
    with pytest.raises(release.ReleaseError, match="^source_inventory_mismatch$"):
        release.source_contract(source_files)


def test_source_snapshot_ignores_generated_cache_but_keeps_package_identity(
    tmp_path: Path, source_files: dict[str, bytes],
) -> None:
    checkout = _source_tree(tmp_path / "source", source_files)
    cache = checkout / "src/video_download_control/__pycache__"
    cache.mkdir()
    (cache / "synthetic.cpython-312.pyc").write_bytes(b"synthetic bytecode")
    assert release.source_snapshot(checkout) == source_files


@pytest.mark.parametrize("name", [
    "runtime-tools/tool.txt", "validation/local/report.md", "notes.jsonl",
    "data-uploads/private/accounts/bilibili/account.json",
    "cookies-private.txt", ".ENV.secret", ".env.not-reviewed.example",
    "download.wav", "download.mov", "download.part", "control.sqlite3-wal",
])
def test_private_runtime_and_media_filenames_are_rejected_even_when_text(name: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.privacy_check({name: b"synthetic fixture, not actual media or credentials"})


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_windows_user_path_in_text_is_rejected_without_a_test_directory_bypass(separator: str) -> None:
    payload = ("C:" + separator + "Users" + separator + "SYNTHETIC-NOT-APPROVED" + separator + "private.txt").encode()
    with pytest.raises(release.ReleaseError, match="^private_user_path$"):
        release.privacy_check({"tests/synthetic.py": payload})


def test_secret_pattern_exception_never_contains_the_candidate_value() -> None:
    canary = b"ghp_" + b"S" * 40
    with pytest.raises(release.ReleaseError) as error:
        release.privacy_check({"docs/example.md": canary})
    assert str(error.value) == "secret_pattern"
    assert canary.decode() not in str(error.value)


@pytest.mark.parametrize("tamper,code", [
    ("payload", "wheel_payload"), ("extra-package-data", "wheel_inventory"),
    ("license", "wheel_payload"), ("entrypoints", "wheel_entrypoints"),
    ("record-hash", "wheel_record"), ("record-size", "wheel_record"),
    ("record-missing", "wheel_record"), ("record-duplicate", "wheel_record"),
    ("record-self-hash", "wheel_record"), ("metadata-license", "archive_metadata"),
    ("metadata-version", "archive_metadata"), ("metadata-license-file", "archive_license_metadata"),
    ("dependency", "runtime_metadata"), ("tag", "wheel_tag"),
    ("wheel-version", "wheel_tag"),
])
def test_wheel_rejects_tampering_even_with_recomputed_record(
    source_files: dict[str, bytes], tamper: str, code: str,
) -> None:
    wheel, project, prefix = _wheel(source_files)
    release.wheel_check(wheel, source_files, project)
    record_name = prefix + "RECORD"
    if tamper == "payload":
        wheel["video_download_control/__init__.py"] += b"# altered\n"
    elif tamper == "extra-package-data":
        wheel["video_download_control/unlisted.json"] = b"{}"
    elif tamper == "license":
        wheel[prefix + "licenses/NOTICE"] += b"altered\n"
    elif tamper == "entrypoints":
        wheel[prefix + "entry_points.txt"] += b"video-download-unexpected = unexpected:main\n"
    elif tamper.startswith("record-"):
        rows = list(csv.reader(io.StringIO(wheel[record_name].decode())))
        normal = next(row for row in rows if row[0] != record_name)
        if tamper == "record-hash":
            normal[1] = "sha256=" + "A" * 43
        elif tamper == "record-size":
            normal[2] = str(int(normal[2]) + 1)
        elif tamper == "record-missing":
            rows.remove(normal)
        elif tamper == "record-duplicate":
            rows.append(list(normal))
        else:
            next(row for row in rows if row[0] == record_name)[1] = "sha256=" + "A" * 43
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        wheel[record_name] = stream.getvalue().encode()
    elif tamper == "metadata-license":
        wheel[prefix + "METADATA"] = wheel[prefix + "METADATA"].replace(b"License-Expression: Apache-2.0", b"License-Expression: LicenseRef-Proprietary")
    elif tamper == "metadata-version":
        wheel[prefix + "METADATA"] = wheel[prefix + "METADATA"].replace(b"Metadata-Version: 2.4", b"Metadata-Version: 1.0")
    elif tamper == "metadata-license-file":
        wheel[prefix + "METADATA"] = wheel[prefix + "METADATA"].replace(b"License-File: NOTICE\n", b"")
    elif tamper == "dependency":
        wheel[prefix + "METADATA"] = wheel[prefix + "METADATA"].replace(b"\n\n", b"\nRequires-Dist: unexpected-package==1.0\n\n", 1)
    elif tamper == "tag":
        wheel[prefix + "WHEEL"] = wheel[prefix + "WHEEL"].replace(b"py3-none-any", b"cp312-cp312-win_amd64")
    else:
        wheel[prefix + "WHEEL"] = wheel[prefix + "WHEEL"].replace(b"Wheel-Version: 1.0", b"Wheel-Version: 2.0")
    if not tamper.startswith("record-"):
        _record(wheel, record_name)
    with pytest.raises(release.ReleaseError, match=f"^{code}$"):
        release.wheel_check(wheel, source_files, project)


def test_source_license_requires_full_apache_text_not_a_label(source_files: dict[str, bytes]) -> None:
    source_files["LICENSE"] = b"Version 2.0, January 2004\nSynthetic incomplete license fixture.\n"
    with pytest.raises(release.ReleaseError, match="^project_license$"):
        release.source_contract(source_files)


@pytest.mark.parametrize("field,code", [
    ("product_identity", "product_identity"),
    ("source_files", "source_hash"),
    ("entrypoint_sha256", "launcher_hash"),
])
def test_verify_rejects_independent_identity_hash_mismatch(
    tmp_path: Path, source_files: dict[str, bytes], field: str, code: str,
) -> None:
    artifacts = tmp_path / "release"
    manifest = _write_release(artifacts, source_files)
    if field == "product_identity":
        manifest[field] = manifest[field].split("+build.sha256.")[0] + "+build.sha256." + "0" * 64
    elif field == "source_files":
        manifest[field]["NOTICE"]["sha256"] = "0" * 64
    else:
        manifest[field][release.ENTRYPOINTS[0]] = "0" * 64
    _write_manifest(artifacts, manifest)
    with pytest.raises(release.ReleaseError, match=f"^{code}$"):
        release.verify_release(artifacts)


def test_cli_secret_failure_does_not_echo_archive_contents_or_location(
    tmp_path: Path, source_files: dict[str, bytes], capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = tmp_path / "SYNTHETIC-PRIVATE-LOCATION"
    manifest = _write_release(artifacts, source_files)
    source_name = manifest["source_zip"]
    source_zip = release.archive_payloads(artifacts / source_name)
    target = next(name for name in source_zip if name.endswith("/start_open_flame.py"))
    canary = b"ghp_" + b"S" * 40
    source_zip[target] += b"\n# " + canary + b"\n"
    _zip(artifacts / source_name, source_zip)
    manifest["artifacts"][source_name] = _fingerprint((artifacts / source_name).read_bytes())
    _write_manifest(artifacts, manifest)
    assert release.main(["verify", "--release-dir", str(artifacts)]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"status": "failed", "error_code": "secret_pattern"}
    assert captured.out == ""
    assert canary.decode() not in captured.err
    assert artifacts.name not in captured.err


def test_build_without_network_confirmation_makes_no_output(tmp_path: Path) -> None:
    output = tmp_path / "not-created"
    with pytest.raises(release.ReleaseError, match="^build_network_confirmation_required$"):
        release.build_release(tmp_path / "not-read", output, wheelhouse=None, allow_network=False)
    assert list(tmp_path.iterdir()) == []


def test_build_never_reuses_or_overwrites_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_bytes(b"synthetic pre-existing release")
    before = _tree_state(tmp_path)
    with pytest.raises(release.ReleaseError, match="^new_absolute_output_required$"):
        release.build_release(tmp_path / "not-read", output, wheelhouse=None, allow_network=True)
    assert _tree_state(tmp_path) == before


def test_verify_cli_runs_without_git_checkout_or_private_validation_tree(
    tmp_path: Path, source_files: dict[str, bytes],
) -> None:
    artifacts = tmp_path / "release"
    manifest = _write_release(artifacts, source_files)
    tool = tmp_path / "standalone" / "scripts" / "release.py"
    tool.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/release.py", tool)
    before = _tree_state(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(tool), "verify", "--release-dir", str(artifacts)],
        cwd=tmp_path, capture_output=True, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, "standalone synthetic release verification failed"
    assert result.stderr == ""
    assert json.loads(result.stdout)["product_identity"] == manifest["product_identity"]
    assert _tree_state(tmp_path) == before
