"""Prepare a Windows source checkout and optional rebuildable AI runtime.

All installation children use the existing bounded process runner. Only the
hash-locked runtime wheels are installed; source is loaded by the root launcher.
An existing environment is read-only unless this installer owns it. No recursive
delete, automatic package upgrade or tool/runtime overwrite is performed here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import sys
from collections.abc import Sequence

from .source_environment_lock import SourceEnvironmentBusy, source_environment_lock
from .local_app_storage import (
    LocalAppLockUnavailable,
    LocalAppStorageError,
    default_local_app_root,
    ensure_plain_directory_tree,
    exclusive_local_app,
    validate_windows_local_path,
)
from .startup_diagnostics import DiagnosticCode, emit_failure
from .subprocess_runner import CommandSpec, SecureSubprocessRunner
from .toolchain import install_toolchain, run_offline_smoke, verify_toolchain


OWNER_FILENAME = "open-flame-source-environment.json"
_OWNER = {"schema_version": 1, "owner": "open-flame-source-setup"}
AI_PYTHON_EMBED_SHA256 = (
    "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
)


class SetupFailure(Exception):
    def __init__(self, code: DiagnosticCode):
        self.code = code
        super().__init__(code.value)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise SetupFailure(DiagnosticCode.INVALID_ARGUMENTS)


def _path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("absolute directory required")
    return path


def _normalized_path(value: str) -> Path:
    path = _path(value)
    if Path(os.path.abspath(path)) != path:
        raise argparse.ArgumentTypeError("normalized absolute path required")
    return path


def _app_root_path(value: str) -> Path:
    path = _normalized_path(value)
    try:
        return validate_windows_local_path(path)
    except LocalAppStorageError:
        raise argparse.ArgumentTypeError("valid local application root required") from None


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Install or repair the Open-Flame Windows source runtime.")
    parser.add_argument("--yes", action="store_true", help="Confirm environment changes and any required PyPI/GitHub downloads.")
    parser.add_argument("--repair", action="store_true", help="Reinstall locked dependencies only in a setup-owned environment.")
    parser.add_argument("--wheelhouse", type=_path, help="Use only local hash-matching dependency wheels (no PyPI access).")
    parser.add_argument("--artifact-cache", type=_path, help="Use only local hash-matching media-tool archives (no GitHub access).")
    parser.add_argument(
        "--ai-python-embed-zip",
        type=_normalized_path,
        metavar="ABSOLUTE_ZIP",
        help="Build the optional AI runtime from the verified CPython 3.13.15 embeddable ZIP.",
    )
    parser.add_argument(
        "--app-root",
        type=_app_root_path,
        metavar="ABSOLUTE_DIR",
        help="AI runtime application root; valid only with --ai-python-embed-zip.",
    )
    return parser


def _plain(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    check = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not check(info.st_mode) or path.is_symlink()
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or (not directory and info.st_nlink != 1)
    ):
        raise OSError("plain local path required")


def _check_prerequisites(repository: Path) -> None:
    if (
        sys.platform != "win32" or platform.python_implementation() != "CPython"
        or sys.version_info < (3, 12) or struct.calcsize("P") != 8
        or platform.machine().lower() not in {"amd64", "x86_64"}
    ):
        raise SetupFailure(DiagnosticCode.SETUP_PREREQUISITE_UNAVAILABLE)
    try:
        _plain(repository, directory=True)
        for relative in ("start_open_flame.py", "Start-Open-Flame.cmd", "pyproject.toml", "deployment/requirements.runtime.lock"):
            _plain(repository / relative, directory=False)
        # A manual invocation from the target interpreter must not repair itself.
        if Path(sys.prefix).resolve() == (repository / ".venv").resolve():
            raise OSError("use an external Python interpreter for setup")
    except OSError:
        raise SetupFailure(DiagnosticCode.SETUP_PREREQUISITE_UNAVAILABLE) from None


def _locked_requirements(path: Path) -> dict[str, str]:
    """Accept the existing closed hash-lock format, never arbitrary pip options."""
    try:
        _plain(path, directory=False)
        payload = path.read_text(encoding="utf-8")
        requirements = {}
        current = None
        hashed = False
        for raw in payload.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+_-]*) \\", line)
            if match:
                if current is not None and not hashed:
                    raise ValueError
                current = match[1].replace("_", "-")
                if current in requirements:
                    raise ValueError
                requirements[current] = match[2]
                hashed = False
            elif current is not None and re.fullmatch(r"--hash=sha256:[0-9a-f]{64}(?: \\)?", line):
                hashed = True
            else:
                raise ValueError
        if not requirements or not hashed:
            raise ValueError
        return requirements
    except (OSError, ValueError):
        raise SetupFailure(DiagnosticCode.SETUP_DEPENDENCIES_FAILED) from None


def _command(python: Path, arguments: Sequence[str], *, repository: Path, timeout: float = 900) -> bool:
    runner = SecureSubprocessRunner(allowed_environment_keys=frozenset({"PIP_CONFIG_FILE"}))
    result = runner.run(CommandSpec(
        executable=python,
        arguments=("-I", *arguments),
        cwd=repository,
        environment={"PIP_CONFIG_FILE": os.devnull},
        timeout_seconds=timeout,
        stdout_limit_bytes=2 * 1024 * 1024,
        stderr_limit_bytes=2 * 1024 * 1024,
    ))
    # Never forward child output, argv, local cache paths or external error text.
    return result.returncode == 0


_RUNTIME_PROBE = """
import importlib.metadata as metadata, json, pathlib, platform, struct, sys
expected = json.loads(sys.argv[1])
root = pathlib.Path(sys.argv[2])
environment = root / '.venv'
assert sys.platform == 'win32' and sys.version_info >= (3, 12)
assert platform.python_implementation() == 'CPython' and struct.calcsize('P') == 8
assert pathlib.Path(sys.prefix).resolve() == environment.resolve()
assert sys.prefix != sys.base_prefix
for name, version in expected.items():
    distribution = metadata.distribution(name)
    assert distribution.version == version
    pathlib.Path(distribution.locate_file('')).resolve().relative_to(environment.resolve())
sys.path.insert(0, str(root / 'src'))
from video_download_control import local_app_cli
assert callable(local_app_cli.main)
"""


def _runtime_ready(python: Path, repository: Path) -> bool:
    expected = _locked_requirements(repository / "deployment/requirements.runtime.lock")
    if not python.is_file():
        return False
    try:
        return _command(python, ("-c", _RUNTIME_PROBE, json.dumps(expected), str(repository)), repository=repository, timeout=30)
    except Exception:
        return False


def _owned_environment(environment: Path) -> bool:
    try:
        _plain(environment, directory=True)
        marker = environment / OWNER_FILENAME
        _plain(marker, directory=False)
        return marker.stat().st_size < 1024 and json.loads(marker.read_text(encoding="utf-8")) == _OWNER
    except (OSError, ValueError):
        return False


def _mark_owned_environment(environment: Path) -> None:
    with (environment / OWNER_FILENAME).open("x", encoding="utf-8") as stream:
        json.dump(_OWNER, stream, separators=(",", ":"))


def _ensure_environment(repository: Path, *, repair: bool, wheelhouse: Path | None) -> Path:
    environment = repository / ".venv"
    python = environment / "Scripts/python.exe"
    _locked_requirements(repository / "deployment/requirements.runtime.lock")
    if os.path.lexists(environment):
        try:
            _plain(environment, directory=True)
        except OSError:
            raise SetupFailure(DiagnosticCode.SETUP_ENVIRONMENT_UNAVAILABLE) from None
        if _runtime_ready(python, repository) and not repair:
            print("Open-Flame setup: existing runtime verified; no dependency changes.", flush=True)
            return python
        if not _owned_environment(environment):
            raise SetupFailure(DiagnosticCode.SETUP_ENVIRONMENT_UNAVAILABLE)
    else:
        environment.mkdir()
        _mark_owned_environment(environment)
        # Keep one readiness probe in the first-install and retry paths alike.
        _runtime_ready(python, repository)
    try:
        print("Open-Flame setup: preparing local Python environment...", flush=True)
        # Never clear or relocate an environment: Windows script launchers embed
        # absolute paths, and a partial owned install must remain retryable.
        if not python.is_file() and not _command(Path(sys.executable), ("-m", "venv", "--without-pip", str(environment)), repository=repository):
            raise SetupFailure(DiagnosticCode.SETUP_ENVIRONMENT_UNAVAILABLE)
        if not _command(python, ("-m", "ensurepip", "--upgrade"), repository=repository):
            raise SetupFailure(DiagnosticCode.SETUP_ENVIRONMENT_UNAVAILABLE)
        arguments = ["-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input", "install", "--no-cache-dir", "--no-deps", "--only-binary=:all:", "--require-hashes", "--force-reinstall"]
        if wheelhouse is None:
            arguments.extend(("--index-url", "https://pypi.org/simple"))
        else:
            _plain(wheelhouse, directory=True)
            arguments.extend(("--no-index", "--find-links", str(wheelhouse)))
        arguments.extend(("--requirement", str(repository / "deployment/requirements.runtime.lock")))
        print("Open-Flame setup: installing locked runtime dependencies...", flush=True)
        if not _command(python, arguments, repository=repository):
            raise SetupFailure(DiagnosticCode.SETUP_DEPENDENCIES_FAILED)
        if not _command(python, ("-m", "pip", "--isolated", "--disable-pip-version-check", "check"), repository=repository):
            raise SetupFailure(DiagnosticCode.SETUP_DEPENDENCIES_FAILED)
        if not _runtime_ready(python, repository):
            raise SetupFailure(DiagnosticCode.SETUP_DEPENDENCIES_FAILED)
    except SetupFailure:
        raise
    except Exception:
        raise SetupFailure(DiagnosticCode.SETUP_DEPENDENCIES_FAILED) from None
    return python


def _prepare_tools(repository: Path, python: Path, artifact_cache: Path | None) -> None:
    root = repository / "runtime-tools/windows-x64"
    try:
        if os.path.lexists(root):
            print("Open-Flame setup: verifying existing media tools...", flush=True)
            verify_toolchain(root, python_executable=python)
            run_offline_smoke(root, python_executable=python)
        else:
            print("Open-Flame setup: installing and checking locked media tools...", flush=True)
            install_toolchain(root, python_executable=python, artifact_cache=artifact_cache)
    except Exception:
        raise SetupFailure(DiagnosticCode.SETUP_TOOLCHAIN_FAILED) from None


_AI_RUNTIME_CHECK = """
import pathlib, sys
repository = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(repository / 'src'))
from video_download_control.editing.ai_runtime_builder import load_current_ai_runtime
runtime = load_current_ai_runtime(pathlib.Path(sys.argv[2]))
assert tuple(runtime.python_identity['version']) == (3, 13, 15)
"""


_AI_RUNTIME_BUILD = """
import pathlib, sys
repository = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(repository / 'src'))
from video_download_control.editing.ai_runtime_builder import build_ai_runtime, load_current_ai_runtime
output = pathlib.Path(sys.argv[3])
runtime = build_ai_runtime(pathlib.Path(sys.argv[2]), sys.argv[4], output)
assert tuple(runtime.python_identity['version']) == (3, 13, 15)
checked = load_current_ai_runtime(output)
assert checked.manifest_sha256 == runtime.manifest_sha256
"""


def _prepare_ai_runtime(
    repository: Path,
    python: Path,
    python_embed_zip: Path,
    app_root: Path | None,
) -> None:
    try:
        root = (
            default_local_app_root(os.environ)
            if app_root is None
            else validate_windows_local_path(app_root)
        )
        ensure_plain_directory_tree(root)
        output = root / "data-ai-runtime"
        with exclusive_local_app(root):
            if os.path.lexists(output):
                if not _command(
                    python,
                    ("-c", _AI_RUNTIME_CHECK, str(repository), str(output)),
                    repository=repository,
                    timeout=120,
                ):
                    raise SetupFailure(DiagnosticCode.SETUP_AI_RUNTIME_FAILED)
                print(
                    "Open-Flame setup: existing AI runtime verified; no runtime changes.",
                    flush=True,
                )
                return
            print(
                "Open-Flame setup: building the optional local AI runtime...",
                flush=True,
            )
            if not _command(
                python,
                (
                    "-c",
                    _AI_RUNTIME_BUILD,
                    str(repository),
                    str(python_embed_zip),
                    str(output),
                    AI_PYTHON_EMBED_SHA256,
                ),
                repository=repository,
                timeout=300,
            ):
                raise SetupFailure(DiagnosticCode.SETUP_AI_RUNTIME_FAILED)
    except LocalAppLockUnavailable:
        raise SetupFailure(DiagnosticCode.SETUP_BUSY) from None
    except SetupFailure:
        raise
    except KeyboardInterrupt:
        raise
    except Exception:
        raise SetupFailure(DiagnosticCode.SETUP_AI_RUNTIME_FAILED) from None


def _install(repository: Path, arguments: argparse.Namespace) -> None:
    try:
        with source_environment_lock(repository, exclusive=True):
            python = _ensure_environment(repository, repair=arguments.repair, wheelhouse=arguments.wheelhouse)
            _prepare_tools(repository, python, arguments.artifact_cache)
            if arguments.ai_python_embed_zip is not None:
                _prepare_ai_runtime(
                    repository,
                    python,
                    arguments.ai_python_embed_zip,
                    arguments.app_root,
                )
    except SourceEnvironmentBusy:
        raise SetupFailure(DiagnosticCode.SETUP_BUSY) from None


def main(argv: Sequence[str] | None = None, *, repository_root: Path) -> int:
    started = False
    try:
        arguments = _parser().parse_args(argv)
        if arguments.app_root is not None and arguments.ai_python_embed_zip is None:
            raise SetupFailure(DiagnosticCode.INVALID_ARGUMENTS)
        _check_prerequisites(repository_root)
        if arguments.ai_python_embed_zip is None:
            print("Open-Flame source setup: prepares .venv and pinned media tools; business data is not changed.", flush=True)
        else:
            print(
                "Open-Flame source setup: prepares .venv and pinned media tools, then creates or verifies the rebuildable AI runtime; business databases and media are not changed.",
                flush=True,
            )
        print("Network: PyPI for missing/repaired dependencies unless --wheelhouse; GitHub for missing tools unless --artifact-cache.", flush=True)
        if arguments.ai_python_embed_zip is not None:
            print(
                "AI runtime: when building, uses only the supplied verified local CPython archive; it performs no provider call.",
                flush=True,
            )
        if not arguments.yes:
            try:
                confirmed = input("Continue? [y/N] ").strip().lower() in {"y", "yes"}
            except EOFError:
                confirmed = False
            if not confirmed:
                print('{"status":"cancelled"}')
                return 130
        started = True
        _install(repository_root, arguments)
        print("Open-Flame source setup complete. Run Start-Open-Flame.cmd to open the app.", flush=True)
        print('{"status":"ready"}')
        return 0
    except KeyboardInterrupt:
        if started:
            emit_failure(DiagnosticCode.SETUP_INTERRUPTED)
        else:
            print('{"status":"cancelled"}')
        return 130
    except SetupFailure as failure:
        emit_failure(failure.code)
        return 2
    except Exception:
        emit_failure(DiagnosticCode.INTERNAL_ERROR)
        return 70


__all__ = ["main"]
