"""Verify a project wheel in a new environment, never in the current checkout.

The release manifest binds artifact bytes; it does not authenticate the publisher. Network dependency
installation requires --allow-network; --wheelhouse always disables the index.
Child processes use the existing bounded runner and its tree-cleanup contract.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tomllib


_SPEC = importlib.util.spec_from_file_location(
    "open_flame_wheel_release_helpers", Path(__file__).with_name("release.py")
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("release_helper_unavailable")
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)

RUNTIME_MODULES = {
    "annotated-doc": "annotated_doc", "annotated-types": "annotated_types",
    "anyio": "anyio", "click": "click", "fastapi": "fastapi", "h11": "h11",
    "idna": "idna", "pydantic": "pydantic", "pydantic-core": "pydantic_core",
    "starlette": "starlette", "typing-extensions": "typing_extensions",
    "typing-inspection": "typing_inspection", "uvicorn": "uvicorn",
}
REPORT_NAME = "wheel-smoke-report.json"
UI_ASSETS = (
    "src/video_download_control/static/open-flame.css",
    "src/video_download_control/static/open-flame-shell.js",
)


class WheelSmokeError(RuntimeError):
    """Only fixed diagnostic codes cross the reporting boundary."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise WheelSmokeError(code)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise WheelSmokeError("invalid_arguments")


def _absolute_plain_directory(path: Path) -> None:
    require(path.is_absolute() and Path(os.path.abspath(path)) == path, "absolute_directory_required")
    for ancestor in reversed((path, *path.parents)):
        release.plain(ancestor, directory=True)


def runtime_requirements(payload: bytes) -> dict[str, str]:
    """Parse only pinned runtime names plus SHA-256 flags, not pip directives."""
    requirements: dict[str, str] = {}
    pending = ""
    for raw in payload.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += " " + (line[:-1].rstrip() if continued else line)
        if continued:
            continue
        match = re.fullmatch(
            r"([a-z0-9][a-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+_-]*)"
            r"(?: --hash=sha256:[0-9a-f]{64})+", pending.strip(),
        )
        require(match is not None, "runtime_lock_invalid")
        name = re.sub(r"[-_.]+", "-", match[1])
        require(name not in requirements, "runtime_lock_invalid")
        requirements[name] = match[2]
        pending = ""
    require(not pending and set(requirements) == set(RUNTIME_MODULES), "runtime_lock_invalid")
    return requirements


INSTALLED_PROBE = r'''
import hashlib, importlib, importlib.metadata as metadata, importlib.resources as resources, json, os, pathlib, sys
expected = json.loads(sys.argv[1])
environment = pathlib.Path(expected['environment']).resolve(strict=True)
work = pathlib.Path(expected['work']).resolve(strict=True)
assert sys.flags.isolated and sys.prefix != sys.base_prefix
assert pathlib.Path(sys.prefix).resolve() == environment

def installed_file(path):
    candidate = pathlib.Path(path).resolve(strict=True)
    candidate.relative_to(environment)
    assert candidate.is_file()
    return candidate

for name, version in expected['runtime'].items():
    distribution = metadata.distribution(name)
    assert distribution.version == version
    pathlib.Path(distribution.locate_file('')).resolve(strict=True).relative_to(environment)
    module = importlib.import_module(expected['runtime_modules'][name])
    installed_file(module.__file__)

distribution = metadata.distribution('video-download-control')
assert distribution.version == expected['version']
pathlib.Path(distribution.locate_file('')).resolve(strict=True).relative_to(environment)
package = importlib.import_module('video_download_control')
installed_file(package.__file__)
assert package.__version__ == expected['version']
identity = importlib.import_module('video_download_control.build_identity')
installed_file(identity.__file__)
assert identity.current_product_identity() == expected['product_identity']
package_root = resources.files('video_download_control')
for relative, expected_sha256 in expected['ui_assets'].items():
    payload = package_root.joinpath(*relative.split('/')).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == expected_sha256
scripts = {entry.name: entry.value for entry in distribution.entry_points if entry.group == 'console_scripts'}
assert scripts == expected['scripts'] and len(scripts) == 14

# This import is from the verified installed wheel, never the verifier checkout.
from video_download_control.subprocess_runner import CommandSpec, SecureSubprocessRunner
runner = SecureSubprocessRunner(allowed_executable_roots=(environment,))
script_root = environment / ('Scripts' if os.name == 'nt' else 'bin')
for name in sorted(scripts):
    executable = installed_file(script_root / (name + ('.exe' if os.name == 'nt' else '')))
    result = runner.run(CommandSpec(
        executable=executable, arguments=('--help',), cwd=work,
        environment={}, timeout_seconds=20,
        stdout_limit_bytes=2 * 1024 * 1024, stderr_limit_bytes=2 * 1024 * 1024,
    ))
    assert result.returncode == 0
'''


def _report() -> dict:
    return {
        "schema_version": 1, "action": "verify_wheel_release", "status": "failed",
        "stage": "policy", "error_code": None, "version": None,
        "product_identity": None, "runtime_dependencies_verified": 0,
        "console_scripts_verified": 0, "ui_assets_verified": 0,
    }


def verify_wheel_release(
    release_dir: Path, work_dir: Path, *, wheelhouse: Path | None = None,
    allow_network: bool = False,
) -> dict:
    report = _report()
    created = False
    try:
        require(wheelhouse is not None or allow_network, "network_confirmation_required")
        _absolute_plain_directory(release_dir)
        require(work_dir.is_absolute() and Path(os.path.abspath(work_dir)) == work_dir
                and not os.path.lexists(work_dir), "new_absolute_work_required")
        _absolute_plain_directory(work_dir.parent)
        require(not work_dir.is_relative_to(release_dir), "work_overlaps_release")
        if wheelhouse is not None:
            _absolute_plain_directory(wheelhouse)

        report["stage"] = "verify_release"
        manifest = release.verify_release(release_dir)
        report.update(version=manifest["version"], product_identity=manifest["product_identity"])
        source = release.archive_payloads(release_dir / manifest["source_zip"])
        prefix = f"Open-Flame-{manifest['version']}-source/"
        selected = {}
        for name in ("deployment/requirements.runtime.lock", "pyproject.toml", *UI_ASSETS):
            payload = source[prefix + name]
            require(release.fingerprint(payload) == manifest["source_files"][name], "release_changed")
            selected[name] = payload
        runtime = runtime_requirements(selected["deployment/requirements.runtime.lock"])
        scripts = tomllib.loads(selected["pyproject.toml"].decode("utf-8"))["project"]["scripts"]
        require(scripts == release.PROJECT_SCRIPTS, "entrypoints_invalid")
        wheel_name = f"video_download_control-{manifest['version']}-py3-none-any.whl"
        wheel_payload = release.read_plain(release_dir, wheel_name)
        require(release.fingerprint(wheel_payload) == manifest["artifacts"][wheel_name], "release_changed")

        report["stage"] = "prepare_work"
        work_dir.mkdir()  # Exclusive creation: never resume or remove an old run.
        created = True
        runtime_lock = work_dir / "requirements.runtime.lock"
        runtime_lock.write_bytes(selected["deployment/requirements.runtime.lock"])
        wheel = work_dir / wheel_name
        wheel.write_bytes(wheel_payload)
        project_lock = work_dir / "requirements.project-wheel.lock"
        project_lock.write_text(
            f"video-download-control @ {wheel.as_uri()} --hash=sha256:"
            f"{manifest['artifacts'][wheel_name]['sha256']}\n", encoding="utf-8", newline="\n",
        )
        environment = work_dir / "wheel-environment"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        report["stage"] = "create_environment"
        release.command(Path(sys._base_executable).resolve(strict=True),
                        ["-m", "venv", "--without-pip", str(environment)], work_dir)
        release.command(python, ["-m", "ensurepip", "--upgrade"], work_dir)
        pip_install = ["-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
                       "install", "--no-cache-dir", "--no-deps", "--only-binary=:all:", "--require-hashes"]
        source_args = (["--no-index", "--find-links", str(wheelhouse)] if wheelhouse is not None
                       else ["--index-url", "https://pypi.org/simple"])
        report["stage"] = "install_runtime"
        release.command(python, [*pip_install, *source_args, "-r", str(runtime_lock)], work_dir)
        report["stage"] = "install_wheel"
        release.command(python, [*pip_install, "--no-index", "-r", str(project_lock)], work_dir)
        report["stage"] = "pip_check"
        release.command(python, ["-m", "pip", "--isolated", "--disable-pip-version-check", "check"], work_dir)
        report["stage"] = "verify_installed_wheel"
        expected = {
            "environment": str(environment), "work": str(work_dir), "runtime": runtime,
            "runtime_modules": RUNTIME_MODULES, "scripts": scripts,
            "version": manifest["version"], "product_identity": manifest["product_identity"],
            "ui_assets": {
                name.removeprefix("src/video_download_control/"):
                    manifest["source_files"][name]["sha256"]
                for name in UI_ASSETS
            },
        }
        release.command(python, ["-c", INSTALLED_PROBE, json.dumps(expected)], work_dir)
        report.update(status="passed", stage="complete", runtime_dependencies_verified=13,
                      console_scripts_verified=len(release.PROJECT_SCRIPTS),
                      ui_assets_verified=len(UI_ASSETS))
    except KeyboardInterrupt:
        report.update(status="cancelled", error_code="cancelled")
    except WheelSmokeError as error:
        report["error_code"] = str(error)
    except Exception:
        report["error_code"] = ("release_verification_failed" if report["stage"] == "verify_release"
                                else "wheel_smoke_failed")
    if created:
        try:
            (work_dir / REPORT_NAME).write_text(json.dumps(report, sort_keys=True) + "\n",
                                               encoding="utf-8", newline="\n")
        except OSError:
            report.update(status="failed", error_code="report_write_failed")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--allow-network", action="store_true")
    try:
        arguments = parser.parse_args(argv)
    except WheelSmokeError:
        report = _report()
        report["error_code"] = "invalid_arguments"
    else:
        report = verify_wheel_release(arguments.release_dir, arguments.work_dir,
                                     wheelhouse=arguments.wheelhouse,
                                     allow_network=arguments.allow_network)
    print(json.dumps(report, sort_keys=True), file=sys.stdout if report["status"] == "passed" else sys.stderr)
    return 0 if report["status"] == "passed" else 130 if report["status"] == "cancelled" else 2


if __name__ == "__main__":
    raise SystemExit(main())
