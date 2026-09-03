#!/usr/bin/env python3
"""Fail-closed provenance and license validation for the candidate tool bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

EXPECTED_TOOLS = {"yt-dlp", "ffmpeg", "ffprobe"}
HEX_DIGITS = frozenset("0123456789abcdef")
TOOL_ARTIFACT_LICENSES = {
    "yt-dlp": {
        "source-entrypoint": {"Unlicense"},
        "unix-zipimport": {"Unlicense AND MIT AND ISC"},
        "pyinstaller-binary": {"GPL-3.0-or-later"},
    },
    "ffmpeg": {
        "self-built-binary": {
            "LGPL-2.1-or-later",
            "GPL-2.0-or-later",
            "GPL-3.0-or-later",
        },
        "distribution-binary": {
            "LGPL-2.1-or-later",
            "GPL-2.0-or-later",
            "GPL-3.0-or-later",
        },
    },
    "ffprobe": {
        "self-built-binary": {
            "LGPL-2.1-or-later",
            "GPL-2.0-or-later",
            "GPL-3.0-or-later",
        },
        "distribution-binary": {
            "LGPL-2.1-or-later",
            "GPL-2.0-or-later",
            "GPL-3.0-or-later",
        },
    },
}
MAX_MANIFEST_BYTES = 256 * 1024
MAX_LICENSE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024


class ToolBundleContractError(ValueError):
    """Raised when a tool bundle cannot provide auditable license evidence."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_file(
    path: Path,
    *,
    label: str,
    max_bytes: int | None = None,
    require_single_link: bool = False,
) -> None:
    requirement = (
        "a regular single-link file"
        if require_single_link
        else "a regular non-symlink file"
    )
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ToolBundleContractError(f"{label} must be {requirement}") from error
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or bool(reparse and getattr(info, "st_file_attributes", 0) & reparse)
        or (require_single_link and info.st_nlink != 1)
    ):
        raise ToolBundleContractError(f"{label} must be {requirement}")
    size = info.st_size
    if size < 1:
        raise ToolBundleContractError(f"{label} must not be empty")
    if max_bytes is not None and size > max_bytes:
        raise ToolBundleContractError(f"{label} exceeds the size limit")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in HEX_DIGITS for character in value)
    )


def _relative_evidence_path(root: Path, raw_path: object, prefix: str) -> Path:
    if not isinstance(raw_path, str) or "\\" in raw_path:
        raise ToolBundleContractError("evidence path must be a POSIX relative path")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != prefix
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ToolBundleContractError(f"evidence path must remain below {prefix}/")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ToolBundleContractError("evidence path must not traverse a symlink")
    return cursor


def _required_text(document: dict[str, object], field: str, *, label: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ToolBundleContractError(f"{label}.{field} must be a bounded string")
    if any(ord(character) < 32 for character in value):
        raise ToolBundleContractError(f"{label}.{field} contains control characters")
    return value.strip()


def _validate_https_source(source_url: str, *, label: str) -> None:
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
        raise ToolBundleContractError(f"{label}.source_url must be an HTTPS URL")


def _validate_hashed_evidence(
    root: Path,
    raw_evidence: object,
    *,
    prefix: str,
    label: str,
    max_bytes: int,
) -> Path:
    if not isinstance(raw_evidence, dict):
        raise ToolBundleContractError(f"{label} must be an object")
    path = _relative_evidence_path(root, raw_evidence.get("path"), prefix)
    expected_hash = raw_evidence.get("sha256")
    if not _is_sha256(expected_hash):
        raise ToolBundleContractError(f"{label}.sha256 must be lowercase SHA-256")
    _plain_file(path, label=label, max_bytes=max_bytes)
    if _sha256(path) != expected_hash:
        raise ToolBundleContractError(f"{label} SHA-256 mismatch")
    return path


def _verify_tool_version(
    root: Path, tool_name: str, version: str, build_configuration: str | None
) -> None:
    flag = "--version" if tool_name == "yt-dlp" else "-version"
    try:
        completed = subprocess.run(
            [str(root / tool_name), flag],
            cwd=root,
            env={"PATH": str(root), "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ToolBundleContractError(f"{tool_name} version check failed") from error
    output = completed.stdout[: 256 * 1024].decode("utf-8", errors="replace")
    lines = output.splitlines()
    if completed.returncode != 0 or not lines:
        raise ToolBundleContractError(f"{tool_name} version check failed")
    if tool_name == "yt-dlp":
        if lines[0].strip() != version:
            raise ToolBundleContractError("yt-dlp version does not match manifest")
        return
    expected_prefix = f"{tool_name} version {version}"
    if not (lines[0] == expected_prefix or lines[0].startswith(expected_prefix + " ")):
        raise ToolBundleContractError(f"{tool_name} version does not match manifest")
    configuration_line = next(
        (line for line in lines if line.strip().startswith("configuration:")), None
    )
    actual_configuration = (
        configuration_line.strip().removeprefix("configuration:").strip()
        if configuration_line is not None
        else ""
    )
    if actual_configuration != (build_configuration or ""):
        raise ToolBundleContractError(
            f"{tool_name} configuration does not match manifest"
        )


def validate_tool_bundle(
    root: Path, *, execute_version_checks: bool = False
) -> dict[str, object]:
    if root.is_symlink() or not root.is_dir():
        raise ToolBundleContractError("tool root must be a regular directory")
    root = root.resolve(strict=True)
    manifest_path = root / "bundle-manifest.json"
    _plain_file(
        manifest_path,
        label="bundle-manifest.json",
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ToolBundleContractError("bundle manifest must be valid UTF-8 JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ToolBundleContractError("bundle manifest schema_version must equal 1")
    _required_text(manifest, "bundle_id", label="manifest")
    tools = manifest.get("tools")
    if not isinstance(tools, dict) or set(tools) != EXPECTED_TOOLS:
        raise ToolBundleContractError("bundle manifest must describe exactly the required tools")

    verified_licenses: set[str] = set()
    for tool_name in sorted(EXPECTED_TOOLS):
        raw_tool = tools[tool_name]
        label = f"tools.{tool_name}"
        if not isinstance(raw_tool, dict):
            raise ToolBundleContractError(f"{label} must be an object")
        version = _required_text(raw_tool, "version", label=label)
        artifact_kind = _required_text(raw_tool, "artifact_kind", label=label)
        source_url = _required_text(raw_tool, "source_url", label=label)
        _validate_https_source(source_url, label=label)
        license_expression = _required_text(
            raw_tool, "license_expression", label=label
        )
        allowed_licenses = TOOL_ARTIFACT_LICENSES[tool_name].get(artifact_kind)
        if allowed_licenses is None:
            raise ToolBundleContractError(f"{label}.artifact_kind is not approved")
        if license_expression not in allowed_licenses:
            raise ToolBundleContractError(
                f"{label}.license_expression is not approved for this artifact kind"
            )
        expected_hash = raw_tool.get("sha256")
        if not _is_sha256(expected_hash):
            raise ToolBundleContractError(f"{label}.sha256 must be lowercase SHA-256")
        source_artifact_hash = raw_tool.get("source_artifact_sha256")
        if not _is_sha256(source_artifact_hash):
            raise ToolBundleContractError(
                f"{label}.source_artifact_sha256 must be lowercase SHA-256"
            )
        source_artifact_path = _required_text(
            raw_tool, "source_artifact_path", label=label
        )
        source_artifact = _relative_evidence_path(
            root, source_artifact_path, "sources"
        )
        _plain_file(
            source_artifact,
            label=f"{label}.source artifact",
            max_bytes=MAX_SOURCE_ARTIFACT_BYTES,
            require_single_link=True,
        )
        if _sha256(source_artifact) != source_artifact_hash:
            raise ToolBundleContractError(
                f"{label}.source artifact SHA-256 mismatch"
            )
        build_configuration: str | None = None
        if tool_name in {"ffmpeg", "ffprobe"}:
            build_configuration = _required_text(
                raw_tool, "build_configuration", label=label
            )
            if "--enable-nonfree" in build_configuration.split():
                raise ToolBundleContractError(
                    f"{label}.build_configuration enables nonfree output"
                )
            if "--enable-gpl" in build_configuration.split() and not license_expression.startswith(
                "GPL-"
            ):
                raise ToolBundleContractError(
                    f"{label}.license_expression conflicts with --enable-gpl"
                )

        executable = root / tool_name
        _plain_file(executable, label=tool_name)
        mode = executable.stat().st_mode
        if not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
            raise ToolBundleContractError(f"{tool_name} must be executable")
        if _sha256(executable) != expected_hash:
            raise ToolBundleContractError(f"{tool_name} SHA-256 mismatch")

        sbom = _validate_hashed_evidence(
            root,
            raw_tool.get("sbom"),
            prefix="sbom",
            label=f"{label}.sbom",
            max_bytes=16 * 1024 * 1024,
        )
        try:
            parsed_sbom = json.loads(sbom.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ToolBundleContractError(f"{label}.sbom must be valid UTF-8 JSON") from error
        if not isinstance(parsed_sbom, dict) or not parsed_sbom:
            raise ToolBundleContractError(f"{label}.sbom must be a non-empty object")
        _validate_hashed_evidence(
            root,
            raw_tool.get("license_review"),
            prefix="reviews",
            label=f"{label}.license_review",
            max_bytes=2 * 1024 * 1024,
        )

        license_files = raw_tool.get("license_files")
        if not isinstance(license_files, list) or not license_files:
            raise ToolBundleContractError(f"{label}.license_files must not be empty")
        if len(license_files) > 1024:
            raise ToolBundleContractError(f"{label}.license_files exceeds the limit")
        for index, raw_license in enumerate(license_files):
            license_label = f"{label}.license_files[{index}]"
            if not isinstance(raw_license, dict):
                raise ToolBundleContractError(f"{license_label} must be an object")
            path = _relative_evidence_path(root, raw_license.get("path"), "licenses")
            expected_license_hash = raw_license.get("sha256")
            if not _is_sha256(expected_license_hash):
                raise ToolBundleContractError(
                    f"{license_label}.sha256 must be lowercase SHA-256"
                )
            _plain_file(path, label=license_label, max_bytes=MAX_LICENSE_BYTES)
            if _sha256(path) != expected_license_hash:
                raise ToolBundleContractError(f"{license_label} SHA-256 mismatch")
            verified_licenses.add(path.relative_to(root).as_posix())

        if execute_version_checks:
            _verify_tool_version(root, tool_name, version, build_configuration)

    return {
        "status": "ok",
        "schema_version": 1,
        "tool_count": len(EXPECTED_TOOLS),
        "license_file_count": len(verified_licenses),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate hashes, provenance and licenses in a VDC tool bundle."
    )
    parser.add_argument("tool_root", type=Path)
    parser.add_argument(
        "--execute-version-checks",
        action="store_true",
        help="Execute each tool and compare its version/configuration with the manifest.",
    )
    arguments = parser.parse_args(argv)
    try:
        result = validate_tool_bundle(
            arguments.tool_root,
            execute_version_checks=arguments.execute_version_checks,
        )
    except (OSError, ToolBundleContractError) as error:
        print(
            json.dumps({"status": "error", "detail": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
