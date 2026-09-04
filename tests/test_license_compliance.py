from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_license_metadata_is_apache_2_0() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    project = pyproject["project"]

    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == [
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.md",
        "licenses/python/*/*",
    ]
    license_text = (ROOT / "LICENSE").read_text("utf-8")
    notice_text = (ROOT / "NOTICE").read_text("utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert notice_text.startswith("Open-Flame (video-download-control)\n")
    assert "Copyright 2026 HedgehogsGX & Cyaegha_Xu" in notice_text


def test_sdist_excludes_self_reference_and_local_runtime_trees() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    sdist = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert sdist["exclude"] == [
        "/validation/apache-2.0-license-migration-evidence.md",
        "/validation/local/**",
        "/runtime-tools/**",
        "/data-uploads/**",
        "/dist/**",
        "/build/**",
        "/.venv/**",
        "/.open-flame-setup.lock",
        "/.pytest_cache/**",
        "/**/__pycache__/**",
    ]


def test_third_party_python_license_copies_match_audited_hash_manifest() -> None:
    manifest_path = ROOT / "compliance" / "python-license-files.sha256"
    expected: dict[str, str] = {}
    for line in manifest_path.read_text("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (licenses/python/.+)", line)
        assert match is not None
        digest, relative_path = match.groups()
        assert relative_path not in expected
        expected[relative_path] = digest

    actual_paths = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "licenses" / "python").glob("*/*")
        if path.is_file()
    }
    assert len(expected) == 29
    assert actual_paths == set(expected)
    for relative_path, digest in expected.items():
        payload = (ROOT / relative_path).read_bytes()
        assert payload
        assert hashlib.sha256(payload).hexdigest() == digest


def test_notices_cover_every_audited_python_distribution() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text("utf-8").casefold()
    manifest = (
        ROOT / "compliance" / "python-license-files.sha256"
    ).read_text("ascii")
    directories = {
        Path(line.split("  ", 1)[1]).parts[2]
        for line in manifest.splitlines()
        if line
    }

    assert len(directories) == 26
    for directory in directories:
        distribution = directory.rsplit("-", 1)[0].replace("_", "-")
        assert distribution.casefold() in notices
    for release_gate in (
        "pydantic-core",
        "oci",
        "yt-dlp",
        "ffmpeg",
        "Apache-2.0",
    ):
        assert release_gate.casefold() in notices
    assert "project-authored source" in notices
    assert "third-party binary and container outputs remain blocked" in notices


def test_remote_cdn_documentation_pages_remain_disabled_in_source() -> None:
    api = (ROOT / "src" / "video_download_control" / "api.py").read_text("utf-8")

    assert "docs_url=None" in api
    assert "redoc_url=None" in api
