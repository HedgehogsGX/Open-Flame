from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "deployment" / "validate_tool_bundle.py"
SPEC = importlib.util.spec_from_file_location("validate_tool_bundle", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_valid_bundle(root: Path) -> dict[str, object]:
    root.mkdir()
    sources = root / "sources"
    sources.mkdir()
    licenses = root / "licenses"
    licenses.mkdir()
    shared_license = licenses / "shared.txt"
    shared_license.write_text("Reviewed license text\n", encoding="utf-8")
    sbom_directory = root / "sbom"
    sbom_directory.mkdir()
    sbom = sbom_directory / "bundle.spdx.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    review_directory = root / "reviews"
    review_directory.mkdir()
    review = review_directory / "license-approval.txt"
    review.write_text("Human review record for offline fixture\n", encoding="utf-8")
    tools: dict[str, object] = {}
    for name in ("yt-dlp", "ffmpeg", "ffprobe"):
        executable = root / name
        executable.write_bytes(f"offline-{name}".encode())
        executable.chmod(0o755)
        source_artifact = sources / f"{name}.source"
        source_artifact.write_bytes(f"reviewed-source-{name}".encode())
        entry: dict[str, object] = {
            "version": "reviewed-version",
            "artifact_kind": (
                "pyinstaller-binary"
                if name == "yt-dlp"
                else "self-built-binary"
            ),
            "source_url": f"https://example.invalid/{name}",
            "source_artifact_path": f"sources/{name}.source",
            "source_artifact_sha256": sha256(source_artifact),
            "sha256": sha256(executable),
            "license_expression": (
                "GPL-3.0-or-later" if name == "yt-dlp" else "LGPL-2.1-or-later"
            ),
            "sbom": {"path": "sbom/bundle.spdx.json", "sha256": sha256(sbom)},
            "license_review": {
                "path": "reviews/license-approval.txt",
                "sha256": sha256(review),
            },
            "license_files": [
                {"path": "licenses/shared.txt", "sha256": sha256(shared_license)}
            ],
        }
        if name in {"ffmpeg", "ffprobe"}:
            entry["build_configuration"] = "--disable-everything"
        tools[name] = entry
    manifest: dict[str, object] = {
        "schema_version": 1,
        "bundle_id": "offline-test-bundle",
        "tools": tools,
    }
    (root / "bundle-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return manifest


def test_tool_bundle_contract_accepts_complete_hash_and_license_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    write_valid_bundle(root)

    result = VALIDATOR.validate_tool_bundle(root)

    assert result == {
        "status": "ok",
        "schema_version": 1,
        "tool_count": 3,
        "license_file_count": 1,
    }


def test_tool_bundle_cli_mode_compares_reported_versions_and_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tools"
    write_valid_bundle(root)
    calls: list[tuple[str, str]] = []

    def completed(command, **_kwargs):
        tool_name = Path(command[0]).name
        calls.append((tool_name, command[1]))
        output = (
            "reviewed-version\n"
            if tool_name == "yt-dlp"
            else (
                f"{tool_name} version reviewed-version\n"
                "configuration: --disable-everything\n"
            )
        )
        return SimpleNamespace(returncode=0, stdout=output.encode())

    monkeypatch.setattr(VALIDATOR.subprocess, "run", completed)

    result = VALIDATOR.validate_tool_bundle(root, execute_version_checks=True)

    assert result["status"] == "ok"
    assert sorted(calls) == [
        ("ffmpeg", "-version"),
        ("ffprobe", "-version"),
        ("yt-dlp", "--version"),
    ]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("malformed-license", "license_expression is not approved"),
        ("missing-source-path", "source_artifact_path must be a bounded string"),
        ("missing-source", "source artifact must be a regular single-link file"),
        ("source-directory", "source artifact must be a regular single-link file"),
        ("source-hash", "source artifact SHA-256 mismatch"),
        ("source-traversal", "evidence path must remain below sources/"),
        ("binary-hash", "yt-dlp SHA-256 mismatch"),
        ("missing-build-config", "build_configuration must be a bounded string"),
        ("nonfree", "build_configuration enables nonfree output"),
        ("license-traversal", "evidence path must remain below licenses/"),
        ("license-hash", r"license_files\[0\] SHA-256 mismatch"),
    ],
)
def test_tool_bundle_contract_fails_closed(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    root = tmp_path / "tools"
    manifest = write_valid_bundle(root)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    if mutation == "malformed-license":
        tools["yt-dlp"]["license_expression"] = "banana"
    elif mutation == "missing-source-path":
        del tools["yt-dlp"]["source_artifact_path"]
    elif mutation == "missing-source":
        (root / "sources" / "yt-dlp.source").unlink()
    elif mutation == "source-directory":
        source = root / "sources" / "yt-dlp.source"
        source.unlink()
        source.mkdir()
    elif mutation == "source-hash":
        (root / "sources" / "yt-dlp.source").write_bytes(b"changed")
    elif mutation == "source-traversal":
        tools["yt-dlp"]["source_artifact_path"] = "../yt-dlp.source"
    elif mutation == "binary-hash":
        (root / "yt-dlp").write_bytes(b"changed")
        if os.name != "nt":
            (root / "yt-dlp").chmod(0o755)
    elif mutation == "missing-build-config":
        del tools["ffmpeg"]["build_configuration"]
    elif mutation == "nonfree":
        tools["ffmpeg"]["build_configuration"] += " --enable-nonfree"
    elif mutation == "license-traversal":
        tools["ffprobe"]["license_files"][0]["path"] = "../LICENSE"
    elif mutation == "license-hash":
        tools["ffprobe"]["license_files"][0]["sha256"] = "0" * 64
    else:  # pragma: no cover - parametrization is intentionally exhaustive
        raise AssertionError(mutation)
    (root / "bundle-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(VALIDATOR.ToolBundleContractError, match=expected):
        VALIDATOR.validate_tool_bundle(root)


def test_tool_bundle_contract_rejects_hardlinked_source_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    write_valid_bundle(root)
    source = root / "sources" / "yt-dlp.source"
    alias = root / "sources" / "yt-dlp.source.alias"
    try:
        os.link(source, alias)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"hardlink creation is unavailable: {error}")

    with pytest.raises(
        VALIDATOR.ToolBundleContractError,
        match="source artifact must be a regular single-link file",
    ):
        VALIDATOR.validate_tool_bundle(root)


def test_tool_bundle_contract_rejects_symlinked_source_artifact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools"
    manifest = write_valid_bundle(root)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    source = root / "sources" / "yt-dlp.source"
    target = root / "sources" / "yt-dlp.source.target"
    source.replace(target)
    try:
        source.symlink_to(target.name)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    tools["yt-dlp"]["source_artifact_sha256"] = sha256(target)
    (root / "bundle-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(
        VALIDATOR.ToolBundleContractError,
        match="evidence path must not traverse a symlink",
    ):
        VALIDATOR.validate_tool_bundle(root)


def test_candidate_dockerfile_executes_license_validator_offline() -> None:
    dockerfile = (ROOT / "deployment" / "Dockerfile.candidate").read_text("utf-8")

    assert "COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./" in dockerfile
    assert "COPY licenses ./licenses" in dockerfile
    assert "COPY deployment/validate_tool_bundle.py" in dockerfile
    assert "python -I /usr/local/libexec/vdc-validate-tool-bundle.py" in dockerfile
    assert "--execute-version-checks /opt/vdc-tools" in dockerfile
    assert "RUN --network=none" in dockerfile
