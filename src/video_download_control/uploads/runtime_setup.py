"""Explicit installer for the optional pinned Windows upload runtime.

Downloads and installation happen only through this command, never on a web
request. The application environment is neither installed into nor upgraded.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..source_environment_lock import SourceEnvironmentBusy, source_environment_lock
from ..windows_job import WindowsKillOnCloseJob


SAU_COMMIT = "0012d2c355f88f683cc38dde2a2db209e14091bc"
SAU_SHA256 = "c647bfd86be8e9c35bd50dafd92b1a1e150a148891b5f856f3505ebe7fe6615c"
SAU_URL = f"https://codeload.github.com/dreammis/social-auto-upload/zip/{SAU_COMMIT}"
BILIUP_VERSION = "v1.2.4"
BILIUP_URL = "https://github.com/biliup/biliup/releases/download/v1.2.4/biliupR-v1.2.4-x86_64-windows.zip"
BILIUP_SHA256 = "cb5af47aeaffd63719c94fa354a4d1404dd8437b6cc215513ec4e6054177c93e"
LOCK_PATH = Path(__file__).with_name("runtime-lock.json")
RUNTIME_MANIFEST_SCHEMA = 2
CHROMIUM_REVISION = "1208"
CHROMIUM_VERSION = "145.0.7632.6"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_NAME = re.compile(r"[-_.]+")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_STATUS_CACHE_SECONDS = 10.0


class SetupError(RuntimeError):
    pass


@contextmanager
def runtime_lock(root: Path, *, exclusive: bool):
    if os.name == "nt":
        try:
            with source_environment_lock(root, exclusive=exclusive):
                yield
        except SourceEnvironmentBusy:
            raise SetupError("runtime_busy") from None
    else:
        # Optional upload runtime is currently Windows-only. This branch keeps
        # offline contract inspection available without pretending browser
        # descendants can be contained by a POSIX process group alone.
        yield


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_file_attributes", 0),
    )


def _content_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    )


def _plain_info(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & reparse
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    ):
        raise ValueError("runtime_redirect_or_type")
    return info


class RuntimeInspectionCache:
    """Process-local digest cache for frequent status polling.

    Cached digests are reused only while the file identity, size and timestamps
    remain unchanged. A complete result is retained briefly so a three-second UI
    poll does not walk thousands of files each time. Workflow admission skips
    the complete-result cache but may reuse a digest only while file identity,
    size, mtime and ctime are unchanged. Platform execution and the setup CLI
    still perform their existing fully uncached checks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._files: dict[str, tuple[tuple[int, ...], str]] = {}
        self._archives: dict[tuple[str, tuple[int, ...], bool], dict[str, str]] = {}
        self._wheels: dict[
            tuple[str, tuple[int, ...]], tuple[str, str, dict[str, str]]
        ] = {}
        self._status: dict[
            str, tuple[tuple[int, ...], float, float, dict[str, object]]
        ] = {}

    def get_status(
        self, runtime: Path, manifest_signature: tuple[int, ...]
    ) -> dict | None:
        key = os.path.normcase(str(runtime.absolute()))
        now = time.monotonic()
        with self._lock:
            cached = self._status.get(key)
            if (
                cached is None
                or cached[0] != manifest_signature
                or cached[2] <= now
            ):
                self._status.pop(key, None)
                return None
            return {
                **cached[3],
                "integrity_cached": True,
                "integrity_age_seconds": round(max(0.0, now - cached[1]), 3),
                "integrity_ttl_seconds": _STATUS_CACHE_SECONDS,
            }

    def put_status(
        self,
        runtime: Path,
        manifest_signature: tuple[int, ...],
        result: dict,
    ) -> None:
        key = os.path.normcase(str(runtime.absolute()))
        with self._lock:
            observed = time.monotonic()
            self._status[key] = (
                manifest_signature,
                observed,
                observed + _STATUS_CACHE_SECONDS,
                dict(result),
            )

    def get_digest(self, path: Path, signature: tuple[int, ...]) -> str | None:
        with self._lock:
            cached = self._files.get(os.path.normcase(str(path.absolute())))
            return cached[1] if cached is not None and cached[0] == signature else None

    def put_digest(self, path: Path, signature: tuple[int, ...], digest: str) -> None:
        with self._lock:
            self._files[os.path.normcase(str(path.absolute()))] = (signature, digest)

    def get_archive(
        self, path: Path, signature: tuple[int, ...], strip_root: bool
    ) -> dict[str, str] | None:
        key = (os.path.normcase(str(path.absolute())), signature, strip_root)
        with self._lock:
            cached = self._archives.get(key)
            return dict(cached) if cached is not None else None

    def put_archive(
        self,
        path: Path,
        signature: tuple[int, ...],
        strip_root: bool,
        files: dict[str, str],
    ) -> None:
        key = (os.path.normcase(str(path.absolute())), signature, strip_root)
        with self._lock:
            for previous in tuple(self._archives):
                if previous[0] == key[0] and previous != key:
                    self._archives.pop(previous, None)
            self._archives[key] = dict(files)
            while len(self._archives) > 8:
                self._archives.pop(next(iter(self._archives)))

    def get_wheel(
        self, path: Path, signature: tuple[int, ...]
    ) -> tuple[str, str, dict[str, str]] | None:
        key = (os.path.normcase(str(path.absolute())), signature)
        with self._lock:
            cached = self._wheels.get(key)
            if cached is None:
                return None
            return cached[0], cached[1], dict(cached[2])

    def put_wheel(
        self,
        path: Path,
        signature: tuple[int, ...],
        details: tuple[str, str, dict[str, str]],
    ) -> None:
        key = (os.path.normcase(str(path.absolute())), signature)
        with self._lock:
            for previous in tuple(self._wheels):
                if previous[0] == key[0] and previous != key:
                    self._wheels.pop(previous, None)
            self._wheels[key] = (details[0], details[1], dict(details[2]))
            while len(self._wheels) > 32:
                self._wheels.pop(next(iter(self._wheels)))


def _file_digest(path: Path, cache: RuntimeInspectionCache | None = None) -> str:
    before = _plain_info(path, directory=False)
    signature = _signature(before)
    if cache is not None:
        cached = cache.get_digest(path, signature)
        if cached is not None:
            return cached
    with path.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        if _content_signature(opened_before) != _content_signature(before) or not stat.S_ISREG(opened_before.st_mode):
            raise ValueError("runtime_file_changed")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        opened_after = os.fstat(stream.fileno())
    after = _plain_info(path, directory=False)
    if (
        _content_signature(opened_after) != _content_signature(before)
        or _content_signature(after) != _content_signature(before)
    ):
        raise ValueError("runtime_file_changed")
    if cache is not None:
        cache.put_digest(path, signature, digest)
    return digest


def _cache_file(path: Path) -> bool:
    return path.suffix.lower() == ".pyc" and "__pycache__" in path.parts


def _tree_inventory(
    root: Path,
    *,
    cache: RuntimeInspectionCache | None = None,
    exclude_manifest: bool = False,
) -> dict[str, str]:
    _plain_info(root, directory=True)
    result: dict[str, str] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name.casefold())
        for entry in ordered:
            path = Path(entry.path)
            info = _plain_info(path, directory=entry.is_dir(follow_symlinks=False))
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
                continue
            relative = path.relative_to(root).as_posix()
            if exclude_manifest and relative == "manifest.json":
                continue
            if _cache_file(path):
                continue
            if path.suffix.lower() == ".pyc":
                raise ValueError("runtime_unscoped_bytecode")
            result[relative] = _file_digest(path, cache)
    return dict(sorted(result.items()))


def _archive_inventory(
    path: Path,
    *,
    strip_root: bool,
    cache: RuntimeInspectionCache | None,
) -> dict[str, str]:
    info = _plain_info(path, directory=False)
    signature = _signature(info)
    if cache is not None:
        cached = cache.get_archive(path, signature, strip_root)
        if cached is not None:
            return cached
    files: dict[str, str] = {}
    total = 0
    with zipfile.ZipFile(path) as bundle:
        for item in bundle.infolist():
            original = PurePosixPath(item.filename)
            parts = original.parts
            if (
                original.is_absolute()
                or ".." in parts
                or any(":" in part or "\\" in part for part in parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("unsafe_archive")
            parts = parts[1:] if strip_root else parts
            if not parts or item.is_dir():
                continue
            relative = PurePosixPath(*parts).as_posix()
            if relative in files:
                raise ValueError("duplicate_archive_member")
            total += item.file_size
            if total > 512 * 1024 * 1024:
                raise ValueError("archive_too_large")
            files[relative] = hashlib.sha256(bundle.read(item)).hexdigest()
    files = dict(sorted(files.items()))
    if cache is not None:
        cache.put_archive(path, signature, strip_root, files)
    return files


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_manifest_key")
        result[key] = value
    return result


def _manifest_map(value) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("invalid_artifact_map")
    result = {}
    for relative, digest in value.items():
        candidate = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            candidate is None
            or candidate.is_absolute()
            or not candidate.parts
            or ".." in candidate.parts
            or "\\" in relative
            or candidate.as_posix() != relative
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise ValueError("invalid_artifact")
        result[relative] = digest
    return dict(sorted(result.items()))


def _canonical_package(name: str) -> str:
    return _PACKAGE_NAME.sub("-", name).lower()


def _locked_packages() -> dict[str, tuple[str, frozenset[str]]]:
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8-sig"), object_pairs_hook=_object_without_duplicates)
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or set(data) != {"schema", "packages", "retrieved"}
        or not isinstance(data["retrieved"], str)
    ):
        raise ValueError("invalid_requirements_lock")
    packages = {}
    if not isinstance(data["packages"], list) or not data["packages"]:
        raise ValueError("invalid_requirements_lock")
    for item in data["packages"]:
        if not isinstance(item, dict) or set(item) != {"name", "version", "sha256", "source"}:
            raise ValueError("invalid_requirements_lock")
        name = _canonical_package(item["name"]) if isinstance(item["name"], str) else ""
        version = item["version"]
        hashes = item["sha256"]
        source = item["source"]
        if (
            not name
            or name in packages
            or not isinstance(version, str)
            or not version
            or not isinstance(hashes, list)
            or not hashes
            or any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in hashes)
            or not isinstance(source, str)
            or not source.startswith("https://pypi.org/pypi/")
        ):
            raise ValueError("invalid_requirements_lock")
        packages[name] = (version, frozenset(hashes))
    return packages


def _wheel_details(
    path: Path, cache: RuntimeInspectionCache | None = None
) -> tuple[str, str, dict[str, str]]:
    info = _plain_info(path, directory=False)
    signature = _signature(info)
    if cache is not None:
        cached = cache.get_wheel(path, signature)
        if cached is not None:
            return cached
    with zipfile.ZipFile(path) as bundle:
        metadata = [
            item for item in bundle.infolist()
            if PurePosixPath(item.filename).parent.name.endswith(".dist-info")
            and PurePosixPath(item.filename).name == "METADATA"
        ]
        if len(metadata) != 1:
            raise ValueError("invalid_wheel_metadata")
        message = BytesParser().parsebytes(bundle.read(metadata[0]))
        raw_name = message.get("Name")
        version = message.get("Version")
        if not isinstance(raw_name, str) or not isinstance(version, str):
            raise ValueError("invalid_wheel_metadata")
        name = _canonical_package(raw_name)
        payloads = {}
        for item in bundle.infolist():
            member = PurePosixPath(item.filename)
            parts = member.parts
            if (
                member.is_absolute()
                or ".." in parts
                or any(":" in part or "\\" in part for part in parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("unsafe_wheel")
            if item.is_dir() or not parts or member.name == "RECORD" and member.parent.name.endswith(".dist-info"):
                continue
            if member.suffix.lower() == ".pyc":
                raise ValueError("wheel_bytecode_forbidden")
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib", "data", "headers", "scripts"}:
                    raise ValueError("unsupported_wheel_layout")
                scheme = parts[1]
                remainder = PurePosixPath(*parts[2:]).as_posix()
                if scheme in {"purelib", "platlib"}:
                    installed = "venv/Lib/site-packages/" + remainder
                elif scheme == "data":
                    installed = "venv/" + remainder
                elif scheme == "headers":
                    header_package = name.replace("-", "_")
                    installed = (
                        "venv/Include/site/python3.12/"
                        f"{header_package}/{remainder}"
                    )
                else:
                    # Pip rewrites #!python scripts and creates platform launchers.
                    # Those generated launchers are inventoried, but the uploader
                    # runtime never executes them after installation.
                    continue
            else:
                installed = "venv/Lib/site-packages/" + member.as_posix()
            digest = hashlib.sha256(bundle.read(item)).hexdigest()
            if installed in payloads and payloads[installed] != digest:
                raise ValueError("duplicate_wheel_payload")
            payloads[installed] = digest
    result = name, version, payloads
    if cache is not None:
        cache.put_wheel(path, signature, result)
    return result


def _validate_wheels(
    runtime: Path,
    packages: dict[str, tuple[str, frozenset[str]]],
    artifacts: dict[str, str],
    cache: RuntimeInspectionCache | None = None,
) -> dict[str, str]:
    wheels = runtime / "wheels"
    _plain_info(wheels, directory=True)
    found = {}
    expected_payloads = {}
    for path in sorted(wheels.glob("*.whl")):
        relative = path.relative_to(runtime).as_posix()
        name, version, payloads = _wheel_details(path, cache)
        if name in found or name not in packages:
            raise ValueError("unexpected_wheel")
        expected_version, allowed = packages[name]
        digest = artifacts.get(relative)
        if version != expected_version or digest not in allowed:
            raise ValueError("unexpected_wheel")
        found[name] = version
        for installed, digest in payloads.items():
            if installed in expected_payloads and expected_payloads[installed] != digest:
                raise ValueError("overlapping_wheel_payload")
            expected_payloads[installed] = digest
    if set(found) != set(packages):
        raise ValueError("missing_wheel")
    return expected_payloads


def _validate_installed_payloads(
    artifacts: dict[str, str], expected_payloads: dict[str, str]
) -> None:
    """Reject importable venv files that are not derived from locked wheels."""
    prefix = "venv/Lib/site-packages/"
    generated_metadata = {"INSTALLER", "RECORD", "REQUESTED", "direct_url.json"}
    for relative in artifacts:
        if not relative.startswith(prefix) or relative in expected_payloads:
            continue
        package_relative = PurePosixPath(relative.removeprefix(prefix))
        if package_relative.parent.name.endswith(".dist-info") and (
            package_relative.name in generated_metadata
        ):
            continue
        raise ValueError("unexpected_installed_payload")


def runtime_python(root: Path) -> Path:
    return root / "runtime" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def isolated_python_command(
    python: Path, entrypoint: Path, pycache: Path, *arguments: str
) -> list[str]:
    """Build the fixed child command without consuming adjacent bytecode."""
    python = Path(python).absolute()
    entrypoint = Path(entrypoint).absolute()
    pycache = Path(pycache).absolute()
    return [
        str(python),
        "-I",
        "-B",
        "-X",
        f"pycache_prefix={pycache}",
        str(entrypoint),
        *arguments,
    ]


def _reject_redirects(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() or current.is_symlink():
            metadata = current.lstat()
            if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise SetupError("runtime_redirect_rejected")
        current = current.parent


def inspect_runtime(
    root: Path,
    *,
    cache: RuntimeInspectionCache | None = None,
    reuse_status: bool = True,
) -> dict:
    runtime = Path(root) / "runtime"
    manifest_path = runtime / "manifest.json"
    if not manifest_path.is_file():
        result = {"ready": False, "code": "runtime_missing"}
        if cache is not None:
            result.update({
                "integrity_cached": False,
                "integrity_age_seconds": 0.0,
                "integrity_ttl_seconds": _STATUS_CACHE_SECONDS,
            })
        return result
    manifest_signature = None
    try:
        _reject_redirects(runtime)
        manifest_info = _plain_info(manifest_path, directory=False)
        manifest_signature = _signature(manifest_info)
        if manifest_info.st_size <= 0 or manifest_info.st_size > _MAX_MANIFEST_BYTES:
            raise ValueError()
        if cache is not None and reuse_status:
            cached = cache.get_status(runtime, manifest_signature)
            if cached is not None:
                return cached
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
        if not isinstance(manifest, dict):
            raise ValueError()
        if manifest.get("schema") == 1:
            result = {"ready": False, "code": "runtime_upgrade_required"}
            if cache is not None:
                result.update({
                    "integrity_cached": False,
                    "integrity_age_seconds": 0.0,
                    "integrity_ttl_seconds": _STATUS_CACHE_SECONDS,
                })
            return result
        required_fields = {
            "schema",
            "sau_commit",
            "sau_archive_sha256",
            "biliup_version",
            "biliup_archive_sha256",
            "requirements_sha256",
            "chromium_revision",
            "cli_help_verified",
            "browser_launch_verified",
            "python",
            "runtime_artifacts",
            "python_artifacts",
        }
        if set(manifest) != required_fields or (
            manifest["schema"] != RUNTIME_MANIFEST_SCHEMA
            or manifest["sau_commit"] != SAU_COMMIT
            or manifest["sau_archive_sha256"] != SAU_SHA256
            or manifest["biliup_version"] != BILIUP_VERSION
            or manifest["biliup_archive_sha256"] != BILIUP_SHA256
            or manifest["requirements_sha256"] != sha256(LOCK_PATH)
            or manifest["chromium_revision"] != CHROMIUM_REVISION
            or manifest["cli_help_verified"] is not True
            or manifest["browser_launch_verified"] is not True
        ):
            raise ValueError()

        artifacts = _manifest_map(manifest["runtime_artifacts"])
        python_artifacts = _manifest_map(manifest["python_artifacts"])
        actual = _tree_inventory(runtime, cache=cache, exclude_manifest=True)
        if actual != artifacts:
            raise ValueError()

        python_relative = runtime_python(Path(root)).relative_to(runtime).as_posix()
        required_runtime = {
            "biliup.exe",
            "chrome/chrome.exe",
            f"chrome/{CHROMIUM_VERSION}.manifest",
            "requirements.lock",
            "source/sau_cli.py",
            "source_revision.txt",
            python_relative,
        }
        if not required_runtime.issubset(artifacts):
            raise ValueError()

        source_archive_relative = (
            f"archives/social-auto-upload-{SAU_COMMIT}.zip"
        )
        biliup_archive_relative = (
            f"archives/biliup-{BILIUP_VERSION}-windows-x64.zip"
        )
        if (
            artifacts.get(source_archive_relative) != SAU_SHA256
            or artifacts.get(biliup_archive_relative) != BILIUP_SHA256
        ):
            raise ValueError()
        source_archive = runtime.joinpath(
            *PurePosixPath(source_archive_relative).parts
        )
        biliup_archive = runtime.joinpath(
            *PurePosixPath(biliup_archive_relative).parts
        )
        expected_source = {
            "source/" + relative: digest
            for relative, digest in _archive_inventory(
                source_archive, strip_root=True, cache=cache
            ).items()
        }
        declared_source = {
            relative: digest
            for relative, digest in artifacts.items()
            if relative.startswith("source/")
        }
        if declared_source != expected_source:
            raise ValueError()
        biliup_entries = [
            digest
            for relative, digest in _archive_inventory(
                biliup_archive, strip_root=False, cache=cache
            ).items()
            if PurePosixPath(relative).name.lower() in {"biliup.exe", "biliupr.exe"}
        ]
        if len(biliup_entries) != 1 or artifacts["biliup.exe"] != biliup_entries[0]:
            raise ValueError()
        if (runtime / "source_revision.txt").read_text(encoding="ascii") != SAU_COMMIT:
            raise ValueError()
        if (runtime / "requirements.lock").read_text(encoding="utf-8") != _requirements():
            raise ValueError()
        packages = _locked_packages()
        expected_wheel_payloads = _validate_wheels(
            runtime, packages, artifacts, cache
        )
        if any(artifacts.get(relative) != digest for relative, digest in expected_wheel_payloads.items()):
            raise ValueError()
        _validate_installed_payloads(artifacts, expected_wheel_payloads)

        python_data = manifest["python"]
        if not isinstance(python_data, dict) or set(python_data) != {
            "implementation",
            "version",
            "bits",
            "base_prefix",
            "base_executable",
        }:
            raise ValueError()
        version = python_data["version"]
        if (
            python_data["implementation"] != "cpython"
            or not isinstance(version, list)
            or len(version) != 3
            or any(type(value) is not int or value < 0 for value in version)
            or version[:2] != [3, 12]
            or python_data["bits"] != 64
            or type(python_data["bits"]) is not int
        ):
            raise ValueError()
        base_prefix = Path(python_data["base_prefix"])
        base_executable = Path(python_data["base_executable"])
        if (
            not isinstance(python_data["base_prefix"], str)
            or not isinstance(python_data["base_executable"], str)
            or not base_prefix.is_absolute()
            or not base_executable.is_absolute()
        ):
            raise ValueError()
        _reject_redirects(base_prefix)
        try:
            executable_relative = base_executable.relative_to(base_prefix).as_posix()
        except ValueError:
            raise ValueError() from None
        actual_python = _tree_inventory(base_prefix, cache=cache)
        if actual_python != python_artifacts or executable_relative not in python_artifacts:
            raise ValueError()
        if not runtime_python(Path(root)).is_file() or not base_executable.is_file():
            raise ValueError()
        pyvenv = (runtime / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
        homes = [
            line.partition("=")[2].strip()
            for line in pyvenv.splitlines()
            if line.partition("=")[0].strip().casefold() == "home"
        ]
        if len(homes) != 1 or os.path.normcase(os.path.abspath(homes[0])) != os.path.normcase(str(base_prefix)):
            raise ValueError()
        if not any(
            relative.startswith("venv/Lib/site-packages/")
            and relative.endswith(".dist-info/METADATA")
            for relative in artifacts
        ):
            raise ValueError()
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        UnicodeError,
        SetupError,
        zipfile.BadZipFile,
    ):
        result = {"ready": False, "code": "runtime_invalid"}
    else:
        result = {"ready": True, "code": "ready"}
    if cache is not None and manifest_signature is not None:
        try:
            current = _signature(_plain_info(manifest_path, directory=False))
            if current == manifest_signature:
                cache.put_status(runtime, manifest_signature, result)
                result = {
                    **result,
                    "integrity_cached": False,
                    "integrity_age_seconds": 0.0,
                    "integrity_ttl_seconds": _STATUS_CACHE_SECONDS,
                }
        except (OSError, ValueError):
            pass
    return result


def _download(url: str, path: Path, expected: str) -> None:
    if path.is_file() and sha256(path) == expected:
        return
    request = urllib.request.Request(url, headers={"User-Agent": "Open-Flame-upload-runtime/1"})
    part = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as target:
        shutil.copyfileobj(response, target)
    if sha256(part) != expected:
        part.unlink()
        raise SetupError("archive_hash_mismatch")
    part.replace(path)


def extract_zip(archive: Path, destination: Path, *, strip_root: bool = False) -> None:
    with zipfile.ZipFile(archive) as bundle:
        total = 0
        for item in bundle.infolist():
            parts = PurePosixPath(item.filename).parts
            if (PurePosixPath(item.filename).is_absolute() or ".." in parts
                    or any(":" in p or "\\" in p for p in parts)
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise SetupError("unsafe_archive")
            parts = parts[1:] if strip_root else parts
            if not parts:
                continue
            total += item.file_size
            if total > 512 * 1024 * 1024:
                raise SetupError("archive_too_large")
            target = destination.joinpath(*parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)


def _environment(runtime: Path) -> dict:
    keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
            "HOME", "LOCALAPPDATA", "APPDATA", "PATH"}
    result = {key: value for key, value in os.environ.items() if key.upper() in keep}
    result.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                   "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                   "PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers")})
    return result


def _command(command: list[str], runtime: Path, code: str, *, timeout: float = 1800) -> None:
    if os.name != "nt":
        raise SetupError("windows_x64_required")
    process = None
    with tempfile.TemporaryDirectory(prefix="command-", dir=runtime) as scratch:
        gate = Path(scratch) / "go"
        wrapped = [sys.executable, "-I", str(Path(__file__).with_name("setup_gate.py")),
                   "--gate", str(gate), *command]
        with WindowsKillOnCloseJob() as job:
            try:
                process = subprocess.Popen(wrapped, cwd=runtime, env=_environment(runtime),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign_pid(process.pid)
                gate.write_text("go", encoding="ascii")
                if process.wait(timeout=timeout):
                    raise SetupError(code)
            except Exception:
                raise SetupError(code) from None
            finally:
                job.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)


def _requirements() -> str:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8-sig"))
    return "\n".join(
        f"{p['name']}=={p['version']} " + " ".join(f"--hash=sha256:{h}" for h in p["sha256"])
        for p in lock["packages"]
    ) + "\n"


def _private_acl(private: Path, runtime: Path) -> None:
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        # The current local user owns the optional accounts and temporary files.
        # No credential values are involved in resolving or applying this ACL.
        name = subprocess.check_output(["whoami"], text=True, encoding="utf-8").strip()
        _command(["icacls", str(private), "/inheritance:r", "/grant:r", f"{name}:(OI)(CI)F"],
                 runtime, "private_acl_failed", timeout=30)
    else:
        private.chmod(0o700)


def verify_cli(root: Path) -> None:
    runtime = root / "runtime"
    python = runtime_python(root)
    with tempfile.TemporaryDirectory(prefix="help-", dir=runtime) as scratch:
        _command(isolated_python_command(
                  python, Path(__file__).with_name("bridge.py"), Path(scratch) / "pycache",
                  "--operation", scratch, "--check-install", str(runtime / "source")),
                 runtime, "cli_help_failed", timeout=60)
    _command([str(runtime / "biliup.exe"), "upload", "--help"], runtime, "biliup_help_failed", timeout=30)
    with browser_view(runtime) as browser:
        _command([str(python), "-I", "-B", "-X", f"pycache_prefix={Path(browser).parent / 'pycache'}", "-c",
              "from patchright.sync_api import sync_playwright; "
              f"p=sync_playwright().start(); b=p.chromium.launch(headless=True,executable_path={str(browser)!r}); "
              "page=b.new_page(); page.set_content('<title>Open-Flame runtime check</title>'); "
              "assert page.title()=='Open-Flame runtime check'; b.close(); p.stop()"],
             runtime, "browser_launch_failed", timeout=60)


def _prepare_short_browser(runtime: Path) -> None:
    # Windows SxS assembly lookup can fail for the deeper Playwright cache path
    # even when CreateProcess's overall path is under MAX_PATH. Use one flat,
    # owned canonical copy; the installer removes the download cache afterwards.
    downloaded = runtime / "browsers" / f"chromium-{CHROMIUM_REVISION}" / "chrome-win64"
    target = runtime / "chrome"
    _plain_info(downloaded, directory=True)
    if target.exists():
        raise SetupError("existing_browser_payload")
    target.mkdir()
    for source in downloaded.rglob("*"):
        path = target / source.relative_to(downloaded)
        if source.is_dir():
            _plain_info(source, directory=True)
            path.mkdir(parents=True, exist_ok=True)
        else:
            _plain_info(source, directory=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, path)
    if not (target / "chrome.exe").is_file() or not (
        target / f"{CHROMIUM_VERSION}.manifest"
    ).is_file():
        raise SetupError("browser_payload_missing")


def _runtime_preseed_is_safe(runtime: Path) -> bool:
    allowed_files = {
        f"archives/social-auto-upload-{SAU_COMMIT}.zip": SAU_SHA256,
        f"archives/biliup-{BILIUP_VERSION}-windows-x64.zip": BILIUP_SHA256,
    }
    allowed_directories = {"archives"}
    try:
        for path in runtime.rglob("*"):
            relative = path.relative_to(runtime).as_posix()
            if path.is_dir():
                _plain_info(path, directory=True)
                if relative not in allowed_directories:
                    return False
            else:
                _plain_info(path, directory=False)
                if relative not in allowed_files or _file_digest(path) != allowed_files[relative]:
                    return False
        return True
    except (OSError, ValueError):
        return False


def _capture_python_identity(root: Path) -> dict:
    runtime = root / "runtime"
    output = runtime / "python-identity.tmp"
    code = (
        "import json,struct,sys; from pathlib import Path; "
        f"Path({str(output)!r}).write_text(json.dumps({{"
        "'implementation':sys.implementation.name,"
        "'version':list(sys.version_info[:3]),"
        "'bits':struct.calcsize('P')*8,"
        "'base_prefix':str(Path(sys.base_prefix).absolute()),"
        "'base_executable':str(Path(sys._base_executable).absolute())"
        "}),encoding='utf-8')"
    )
    try:
        _command(
            [
                str(runtime_python(root)),
                "-I",
                "-B",
                "-X",
                f"pycache_prefix={runtime / 'identity-pycache'}",
                "-c",
                code,
            ],
            runtime,
            "python_identity_failed",
            timeout=30,
        )
        if not output.is_file() or output.stat().st_size > 16_384:
            raise SetupError("python_identity_failed")
        data = json.loads(
            output.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    finally:
        output.unlink(missing_ok=True)
    if not isinstance(data, dict):
        raise SetupError("python_identity_failed")
    return data


def _write_runtime_manifest(root: Path, python_data: dict) -> None:
    runtime = root / "runtime"
    if (runtime / "manifest.json").exists():
        raise SetupError("existing_runtime_manifest")
    base_prefix = Path(python_data.get("base_prefix", ""))
    base_executable = Path(python_data.get("base_executable", ""))
    version = python_data.get("version")
    if (
        python_data.get("implementation") != "cpython"
        or not isinstance(version, list)
        or len(version) != 3
        or version[:2] != [3, 12]
        or any(type(value) is not int for value in version)
        or python_data.get("bits") != 64
        or not base_prefix.is_absolute()
        or not base_executable.is_absolute()
    ):
        raise SetupError("python_identity_failed")
    _reject_redirects(base_prefix)
    try:
        base_executable.relative_to(base_prefix)
    except ValueError:
        raise SetupError("python_identity_failed") from None
    runtime_artifacts = _tree_inventory(runtime, exclude_manifest=True)
    python_artifacts = _tree_inventory(base_prefix)
    manifest = {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "sau_commit": SAU_COMMIT,
        "sau_archive_sha256": SAU_SHA256,
        "biliup_version": BILIUP_VERSION,
        "biliup_archive_sha256": BILIUP_SHA256,
        "requirements_sha256": sha256(LOCK_PATH),
        "chromium_revision": CHROMIUM_REVISION,
        "cli_help_verified": True,
        "browser_launch_verified": True,
        "python": python_data,
        "runtime_artifacts": runtime_artifacts,
        "python_artifacts": python_artifacts,
    }
    temporary = runtime / "manifest.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(runtime / "manifest.json")


@contextmanager
def browser_view(runtime: Path):
    """An owned binary-only temporary view; no account or browser profile here.

    The 145 Windows SxS loader failed for the normal application's storage path
    on the verified host. An independently owned temporary hardlink view of the
    same bytes launches correctly without changing system/browser settings.
    """
    base = Path(tempfile.mkdtemp(prefix="of-browser-"))
    try:
        for source in (runtime / "chrome").rglob("*"):
            target = base / source.relative_to(runtime / "chrome")
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
        yield base / "chrome.exe"
    finally:
        for attempt in range(40):
            try:
                shutil.rmtree(base)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt < 39:
                    time.sleep(.05)


def install(root: Path, python: Path) -> dict:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SetupError("windows_x64_required")
    if not python.is_absolute() or not python.is_file():
        raise SetupError("python_absolute_path_required")
    for directory in (root, root / "runtime", root / "private"):
        _reject_redirects(directory)
    root.mkdir(parents=True, exist_ok=True)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    previous = inspect_runtime(root)
    if previous["ready"]:
        verify_cli(root)
        if not inspect_runtime(root)["ready"]:
            raise SetupError("runtime_changed_during_verification")
        return {"ready": True, "code": "runtime_already_ready"}
    if previous["code"] == "runtime_upgrade_required":
        raise SetupError("runtime_upgrade_requires_reinstall")
    if previous["code"] == "runtime_invalid":
        raise SetupError("runtime_invalid_requires_reinstall")
    if not _runtime_preseed_is_safe(runtime):
        raise SetupError("runtime_partial_requires_reinstall")
    _private_acl(root / "private", runtime)
    _command([str(python), "-I", "-B", "-c",
              "import sys,struct; assert sys.implementation.name=='cpython' and sys.version_info[:2]==(3,12) and struct.calcsize('P')==8"],
             runtime, "cpython_312_x64_required", timeout=30)
    print("Preparing pinned source archives", flush=True)
    cache = runtime / "archives"
    cache.mkdir(exist_ok=True)
    source_zip = cache / f"social-auto-upload-{SAU_COMMIT}.zip"
    bili_zip = cache / f"biliup-{BILIUP_VERSION}-windows-x64.zip"
    _download(SAU_URL, source_zip, SAU_SHA256)
    _download(BILIUP_URL, bili_zip, BILIUP_SHA256)
    source = runtime / "source"
    if source.exists():
        raise SetupError("runtime_partial_requires_reinstall")
    source.mkdir()
    extract_zip(source_zip, source, strip_root=True)
    (runtime / "source_revision.txt").write_text(SAU_COMMIT, encoding="ascii")
    expected_source = _archive_inventory(source_zip, strip_root=True, cache=None)
    if _tree_inventory(source) != expected_source:
        raise SetupError("source_integrity_failed")
    if (runtime / "biliup.exe").exists():
        raise SetupError("runtime_partial_requires_reinstall")
    with tempfile.TemporaryDirectory(prefix="biliup-", dir=runtime) as scratch:
        extracted = Path(scratch)
        extract_zip(bili_zip, extracted)
        binaries = [p for p in extracted.rglob("*.exe") if p.name.lower() in {"biliup.exe", "biliupr.exe"}]
        if len(binaries) != 1:
            raise SetupError("biliup_binary_missing")
        shutil.copyfile(binaries[0], runtime / "biliup.exe")
    print("Preparing isolated CPython 3.12 environment", flush=True)
    if (runtime / "venv").exists():
        raise SetupError("runtime_partial_requires_reinstall")
    _command([str(python), "-I", "-B", "-m", "venv", str(runtime / "venv")], runtime, "venv_creation_failed")
    requirement_path = runtime / "requirements.lock"
    requirement_path.write_text(_requirements(), encoding="utf-8")
    wheels = runtime / "wheels"
    wheels.mkdir()
    _command([str(runtime_python(root)), "-I", "-B", "-m", "pip", "download",
              "--require-hashes", "--only-binary=:all:", "--no-deps",
              "--index-url", "https://pypi.org/simple", "--dest", str(wheels),
              "-r", str(requirement_path)], runtime, "dependency_download_failed")
    wheel_artifacts = {
        "wheels/" + relative: digest
        for relative, digest in _tree_inventory(wheels).items()
    }
    _validate_wheels(runtime, _locked_packages(), wheel_artifacts)
    _command([str(runtime_python(root)), "-I", "-B", "-m", "pip", "install",
              "--require-hashes", "--only-binary=:all:", "--no-deps", "--no-index",
              "--find-links", str(wheels), "-r", str(requirement_path)],
             runtime, "dependency_install_failed")
    _command([str(runtime_python(root)), "-I", "-B", "-m", "pip", "check"],
             runtime, "dependency_check_failed", timeout=60)
    print("Preparing pinned Chromium runtime", flush=True)
    _command([str(runtime_python(root)), "-I", "-B", "-m", "patchright", "install", "chromium", "--no-shell"],
             runtime, "browser_install_failed")
    _prepare_short_browser(runtime)
    shutil.rmtree(runtime / "browsers")
    # Pip is needed only while assembling the runtime. Removing it makes every
    # importable site-packages payload traceable to the pinned wheel lock.
    _command([str(runtime_python(root)), "-I", "-B", "-m", "pip", "uninstall",
              "--yes", "pip"], runtime, "pip_removal_failed", timeout=60)
    print("Checking uploader command contracts and local browser launch", flush=True)
    verify_cli(root)
    python_data = _capture_python_identity(root)
    _write_runtime_manifest(root, python_data)
    result = inspect_runtime(root)
    if not result["ready"]:
        (runtime / "manifest.json").unlink(missing_ok=True)
        raise SetupError("runtime_manifest_verification_failed")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Absolute path to an existing CPython 3.12 x64 interpreter")
    parser.add_argument("--check", action="store_true", help="Verify existing runtime; never install or log in")
    args = parser.parse_args(argv)
    if not args.root.is_absolute():
        parser.error("--root must be absolute")
    try:
        _reject_redirects(args.root)
        if args.check:
            if not args.root.is_dir():
                result = {"ready": False, "code": "runtime_missing"}
            else:
                with runtime_lock(args.root, exclusive=False):
                    result = inspect_runtime(args.root)
                    if result["ready"]:
                        verify_cli(args.root)
                        result = inspect_runtime(args.root)
        else:
            if args.python is None:
                parser.error("--python is required for installation")
            args.root.mkdir(parents=True, exist_ok=True)
            with runtime_lock(args.root, exclusive=True):
                result = install(args.root, args.python)
    except SetupError as exc:
        result = {"ready": False, "code": str(exc)}
    except Exception:
        result = {"ready": False, "code": "runtime_setup_failed"}
    print(json.dumps(result))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
