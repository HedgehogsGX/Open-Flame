"""Build and inspect project-only releases without Git or historical verifiers.

The explicit release-files.txt is the review boundary. Reports bind bytes, not
publisher authenticity. Nothing here uploads a release or grants legal approval.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import csv
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
import tarfile
import tomllib
from urllib.parse import unquote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("Setup-Open-Flame.cmd", "setup_open_flame.py", "Start-Open-Flame.cmd", "start_open_flame.py")
PROJECT_SCRIPTS = {
    "video-download-control": "video_download_control.cli:main",
    "video-download-worker": "video_download_control.worker_cli:main",
    "video-download-validation": "video_download_control.validation_cli:main",
    "video-download-capabilities": "video_download_control.capability_cli:main",
    "video-download-unix-relay": "video_download_control.relay_cli:main",
    "video-download-egress-proxy": "video_download_control.egress_proxy_cli:main",
    "video-download-short-link-egress": "video_download_control.short_link_transport_cli:main",
    "video-download-candidate-worker": "video_download_control.candidate_worker_cli:main",
    "video-download-credentials": "video_download_control.credential_cli:main",
    "video-download-backup": "video_download_control.backup_cli:main",
    "video-upload-backup": "video_download_control.upload_backup_cli:main",
    "video-download-tools": "video_download_control.toolchain_cli:main",
    "video-download-local-worker": "video_download_control.local_worker_cli:main",
    "video-download-local-app": "video_download_control.local_app_cli:main",
}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRIES = 2000
EXCLUDED_REPORT = "validation/apache-2.0-license-migration-evidence.md"
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", "runtime-tools", "data", "data-edits", "data-uploads", "dist", "build", ".pytest_cache", "node_modules"}
FORBIDDEN_SUFFIXES = {".exe", ".dll", ".pyd", ".pyc", ".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".mp4", ".mp3", ".webm", ".mkv", ".mov", ".wav", ".avi", ".flac", ".m4a", ".part", ".ytdl", ".pem", ".key", ".p12", ".pfx", ".zip", ".whl"}
ENV_EXAMPLES = {".env.example", "deployment/.env.candidate.example", "deployment/cookies/cookie-sources.env.example"}
SECRET_PATTERNS = (
    rb"gh[pousr]_[A-Za-z0-9]{30,}", rb"github_pat_[A-Za-z0-9_]{50,}",
    rb"AKIA[A-Z0-9]{16}", rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
)


class ReleaseError(RuntimeError):
    """Fixed, non-sensitive release validation failure."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise ReleaseError(code)


def fingerprint(payload: bytes) -> dict:
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def safe_name(name: str) -> str:
    require(isinstance(name, str) and name and len(name) <= 240, "unsafe_member")
    parts = name.split("/")
    require(not any(c in name for c in '\\:<>"|?*') and not any(ord(c) < 32 for c in name), "unsafe_member")
    require(all(p not in {"", ".", ".."} and not p.endswith((".", " ")) for p in parts), "unsafe_member")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    require(not any(p.split(".", 1)[0].casefold() in reserved for p in parts), "unsafe_member")
    return name


def plain(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    require(not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0), "linked_path")
    require((stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode), "non_plain_path")
    if not directory:
        require(info.st_nlink == 1, "linked_path")


def read_plain(root: Path, name: str) -> bytes:
    safe_name(name)
    plain(root, directory=True)
    target = root
    parts = name.split("/")
    for part in parts[:-1]:
        target /= part
        plain(target, directory=True)
    target /= parts[-1]
    plain(target)
    require(target.stat().st_size <= MAX_FILE, "file_too_large")
    payload = target.read_bytes()
    require(len(payload) <= MAX_FILE, "file_too_large")
    return payload


def file_list(payload: bytes) -> list[str]:
    names = [line for line in payload.decode("utf-8").splitlines() if line and not line.startswith("#")]
    require(0 < len(names) <= MAX_ENTRIES, "invalid_inventory")
    require(len({safe_name(name).casefold() for name in names}) == len(names), "duplicate_inventory")
    return sorted(names)


def privacy_check(files: dict[str, bytes]) -> None:
    for name, payload in files.items():
        parts = PurePosixPath(name).parts
        lowered = name.casefold()
        require(not FORBIDDEN_PARTS.intersection(p.casefold() for p in parts), "private_path")
        require(not lowered.startswith("validation/local/") and name != EXCLUDED_REPORT, "private_path")
        require(PurePosixPath(lowered).suffix not in FORBIDDEN_SUFFIXES, "private_file_type")
        require(not re.search(r"\.(?:db|sqlite|sqlite3)-[^/]+$", lowered), "private_file_type")
        require(not any(p.casefold().startswith(".env") or p.casefold().endswith(".env.example") for p in parts) or name in ENV_EXAMPLES, "private_environment")
        require(not re.search(r"(?:^|/)cookies?[^/]*\.(?:txt|json)$", lowered), "private_cookies")
        require(b"\x00" not in payload, "non_text_payload")
        payload.decode("utf-8")
        require(not any(re.search(pattern, payload) for pattern in SECRET_PATTERNS), "secret_pattern")
        # Only the already-reviewed, explicitly synthetic Windows path canaries
        # are allowed. Do not turn the entire tests tree into a privacy bypass.
        for match in re.finditer(rb"(?i)[A-Z]:[\\/]+Users[\\/]+([^\\/\s\"'`]+)", payload):
            require(match[1] in {b"PRIVATE-CANARY", b"PRIVATE-PATH-CANARY", b"PRIVATE-DRIFT-CANARY", b"PRIVATE-UNAVAILABLE-CANARY", b"PRIVATE-*-CANARY"}, "private_user_path")
        require(not re.search(rb"wxid_[A-Za-z0-9_]+|" + b"xwechat" + b"_files", payload), "private_identifier")


def documentation_targets(files: dict[str, bytes]) -> set[str]:
    """Collect literal local links into the shipped documentation trees.

    This bounded inventory check covers inline Markdown file links, excluding
    fenced examples and external URLs. Checkout-only tests and ignored local
    evidence are outside the distribution's documentation contract.
    """
    targets = set()
    for name, payload in files.items():
        if not name.endswith(".md"):
            continue
        text = re.sub(
            r"(?ms)^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*(?=\n|$)",
            "",
            payload.decode("utf-8"),
        )
        for match in re.finditer(
            r"\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:[ \t]+[^\n)]*)?\)",
            text,
        ):
            reference = urlsplit(match.group(1) or match.group(2))
            if reference.scheme or reference.netloc or not reference.path:
                continue
            target = posixpath.normpath(posixpath.join(
                posixpath.dirname(name), unquote(reference.path)
            ))
            if (
                target.startswith(("docs/", "validation/"))
                and not target.startswith("validation/local/")
                and target != EXCLUDED_REPORT
                and PurePosixPath(target).suffix
            ):
                targets.add(target)
    return targets


def validate_documentation_inventory(files: dict[str, bytes]) -> None:
    """Require complete documentation when preparing a new source release.

    Existing archives retain their declared inventory/identity contract. This
    authoring check must not turn archive verification into a dependency on the
    current checkout's documentation history.
    """
    require(documentation_targets(files) <= files.keys(), "unlisted_documentation_link")


def source_contract(files: dict[str, bytes]) -> tuple[dict, str]:
    require(set(files) == set(file_list(files["release-files.txt"])), "source_inventory_mismatch")
    privacy_check(files)
    config = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
    project = config["project"]
    version = project["version"]
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "invalid_version")
    require(project["name"] == "video-download-control" and project["license"] == "Apache-2.0", "project_metadata")
    require(b'__version__ = "' + version.encode() + b'"' in files["src/video_download_control/__init__.py"], "version_mismatch")
    # Audited Apache-2.0 text, allowing only platform newline normalization.
    require(hashlib.sha256(files["LICENSE"].replace(b"\r\n", b"\n")).hexdigest() == "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4", "project_license")
    require(files["NOTICE"].decode("utf-8").splitlines() == ["Open-Flame (video-download-control)", "Copyright 2026 HedgehogsGX & Cyaegha_Xu"], "project_notice")
    legal = {}
    for line in files["compliance/python-license-files.sha256"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (licenses/python/.+)", line)
        require(match is not None, "license_manifest")
        digest, name = match.groups()
        require(name not in legal and name in files and hashlib.sha256(files[name]).hexdigest() == digest, "license_hash")
        legal[name] = digest
    require(set(legal) == {name for name in files if name.startswith("licenses/python/")}, "license_inventory")
    require(len(legal) == 30, "license_inventory")
    require(project["license-files"] == ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "licenses/python/*/*"], "license_metadata")
    require(project.get("scripts") == PROJECT_SCRIPTS, "entrypoint_metadata")
    for name in ENTRYPOINTS:
        require(name in files and files[name], "missing_launcher")
    digest = hashlib.sha256()
    prefix = "src/video_download_control/"
    for name in sorted(n for n in files if n.startswith(prefix)):
        relative = name.removeprefix(prefix).encode("utf-8")
        payload = files[name]
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return project, f"{version}+build.sha256.{digest.hexdigest()}"


def source_snapshot(root: Path) -> dict[str, bytes]:
    files = {name: read_plain(root, name) for name in file_list(read_plain(root, "release-files.txt"))}
    source_contract(files)
    # A new importable module/data file cannot silently be omitted by the list.
    package = root / "src/video_download_control"
    actual = set()
    for path in package.rglob("*"):
        if "__pycache__" in path.parts and path.suffix == ".pyc":
            continue
        plain(path, directory=path.is_dir())
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    require(actual == {n for n in files if n.startswith("src/video_download_control/")}, "unlisted_package_file")
    return files


def archive_payloads(path: Path) -> dict[str, bytes]:
    plain(path)
    require(path.stat().st_size <= MAX_TOTAL, "archive_too_large")
    result: dict[str, bytes] = {}
    folded: set[str] = set()
    total = 0

    def add(name: str, size: int, read) -> None:
        nonlocal total
        safe_name(name)
        require(name.casefold() not in folded, "duplicate_member")
        require(len(result) < MAX_ENTRIES and 0 <= size <= MAX_FILE, "archive_limit")
        total += size
        require(total <= MAX_TOTAL, "archive_limit")
        payload = read()
        require(len(payload) == size, "member_size")
        result[name] = payload
        folded.add(name.casefold())

    if path.name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(path) as archive:
            require(not archive.comment, "archive_private_metadata")
            require(len(archive.infolist()) <= MAX_ENTRIES, "archive_limit")
            for info in archive.infolist():
                require(info.orig_filename == info.filename, "unsafe_member")
                mode = info.external_attr >> 16
                require(not info.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG), "non_regular_member")
                require(not info.flag_bits & 1, "encrypted_member")
                require(not info.comment and not info.extra, "archive_private_metadata")
                add(info.filename, info.file_size, lambda i=info: archive.read(i))
    else:
        require(path.name.endswith(".tar.gz"), "archive_type")
        with tarfile.open(path, "r:gz") as archive:
            count = 0
            for info in archive:
                count += 1
                require(count <= MAX_ENTRIES, "archive_limit")
                require(info.isfile() and not info.issparse(), "non_regular_member")
                require(not info.uname and not info.gname and info.uid == info.gid == 0, "archive_private_metadata")
                require(not set(info.pax_headers) - {"path", "mtime"}, "archive_private_metadata")
                add(info.name, info.size, lambda i=info: archive.extractfile(i).read())
    return result


def metadata_check(payload: bytes, project: dict, legal_names: set[str]) -> None:
    metadata = BytesParser().parsebytes(payload)
    require(metadata.get_all("Metadata-Version") == ["2.4"], "archive_metadata")
    for field, value in (("Name", project["name"]), ("Version", project["version"]), ("License-Expression", "Apache-2.0"), ("Requires-Python", project["requires-python"])):
        require(metadata.get_all(field) == [value], "archive_metadata")
    require(set(metadata.get_all("License-File", [])) == legal_names, "archive_license_metadata")
    requirements = set(metadata.get_all("Requires-Dist", []))
    expected = set(project["dependencies"])
    for extra, dependencies in project.get("optional-dependencies", {}).items():
        expected.update(dependency + f"; extra == '{extra}'" for dependency in dependencies)
    require(requirements == expected, "runtime_metadata")


def wheel_check(wheel: dict[str, bytes], source: dict[str, bytes], project: dict) -> None:
    require(project.get("scripts") == PROJECT_SCRIPTS, "entrypoint_metadata")
    prefix = f"video_download_control-{project['version']}.dist-info/"
    legal = {n for n in source if n in {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"} or n.startswith("licenses/python/")}
    expected = {n.removeprefix("src/"): b for n, b in source.items() if n.startswith("src/video_download_control/")}
    expected.update({prefix + "licenses/" + n: source[n] for n in legal})
    generated = {prefix + n for n in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")}
    require(set(wheel) == set(expected) | generated, "wheel_inventory")
    require(all(wheel[n] == b for n, b in expected.items()), "wheel_payload")
    metadata_check(wheel[prefix + "METADATA"], project, legal)
    tags = BytesParser().parsebytes(wheel[prefix + "WHEEL"])
    require(tags.get_all("Wheel-Version") == ["1.0"] and tags.get_all("Tag") == ["py3-none-any"] and tags.get_all("Root-Is-Purelib") == ["true"], "wheel_tag")
    entrypoints = configparser.ConfigParser(interpolation=None)
    entrypoints.read_string(wheel[prefix + "entry_points.txt"].decode())
    require(entrypoints.sections() == ["console_scripts"] and dict(entrypoints["console_scripts"]) == PROJECT_SCRIPTS, "wheel_entrypoints")
    rows = list(csv.reader(io.StringIO(wheel[prefix + "RECORD"].decode("utf-8"))))
    require(len(rows) == len(wheel) and all(len(row) == 3 for row in rows), "wheel_record")
    require({row[0] for row in rows} == set(wheel), "wheel_record")
    for name, digest, size in rows:
        if name == prefix + "RECORD":
            require(digest == size == "", "wheel_record")
        else:
            expected_hash = base64.urlsafe_b64encode(hashlib.sha256(wheel[name]).digest()).rstrip(b"=").decode("ascii")
            require(digest == "sha256=" + expected_hash and size == str(len(wheel[name])), "wheel_record")


def verify_release(release_dir: Path) -> dict:
    manifest = json.loads(read_plain(release_dir, "release-manifest.json"))
    require(manifest["schema_version"] == 1, "manifest_schema")
    version = manifest["version"]
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), "invalid_version")
    source_zip = f"Open-Flame-{version}-source.zip"
    wheel_name = f"video_download_control-{version}-py3-none-any.whl"
    sdist_name = f"video_download_control-{version}.tar.gz"
    expected_artifacts = {source_zip, wheel_name, sdist_name}
    require(manifest["source_zip"] == source_zip and set(manifest["artifacts"]) == expected_artifacts, "artifact_inventory")
    require({p.name for p in release_dir.iterdir()} == expected_artifacts | {"release-manifest.json", "SHA256SUMS"}, "release_directory_inventory")
    archives = {}
    for name in sorted(expected_artifacts):
        payload = read_plain(release_dir, name)
        require(fingerprint(payload) == manifest["artifacts"][name], "artifact_hash")
        archives[name] = archive_payloads(release_dir / name)
    prefix = f"Open-Flame-{version}-source/"
    require(all(n.startswith(prefix) for n in archives[source_zip]), "source_root")
    source = {n.removeprefix(prefix): b for n, b in archives[source_zip].items()}
    project, identity = source_contract(source)
    require(project["version"] == version and identity == manifest["product_identity"], "product_identity")
    require({n: fingerprint(b) for n, b in source.items()} == manifest["source_files"], "source_hash")
    require({n: hashlib.sha256(source[n]).hexdigest() for n in ENTRYPOINTS} == manifest["entrypoint_sha256"], "launcher_hash")
    sdist_prefix = f"video_download_control-{version}/"
    expected = {sdist_prefix + n: b for n, b in source.items()}
    sdist = archives[sdist_name]
    require(set(sdist) == set(expected) | {sdist_prefix + "PKG-INFO"}, "sdist_inventory")
    require(all(sdist[n] == b for n, b in expected.items()), "sdist_payload")
    legal = {n for n in source if n in {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"} or n.startswith("licenses/python/")}
    metadata_check(sdist[sdist_prefix + "PKG-INFO"], project, legal)
    wheel_check(archives[wheel_name], source, project)
    sums = "".join(f"{manifest['artifacts'][n]['sha256']}  {n}\n" for n in sorted(expected_artifacts))
    sums += f"{hashlib.sha256(read_plain(release_dir, 'release-manifest.json')).hexdigest()}  release-manifest.json\n"
    require(read_plain(release_dir, "SHA256SUMS") == sums.encode("ascii"), "checksums")
    return manifest


def command(python: Path, arguments: list[str], cwd: Path) -> None:
    # These are stdlib-only project helpers, available in a fresh source unzip.
    sys.path.insert(0, str(ROOT / "src"))
    from video_download_control.subprocess_runner import CommandSpec, SecureSubprocessRunner
    runner = SecureSubprocessRunner(allowed_environment_keys=frozenset({"PIP_CONFIG_FILE", "SOURCE_DATE_EPOCH"}))
    result = runner.run(CommandSpec(executable=python, arguments=("-I", *arguments), cwd=cwd,
        environment={"PIP_CONFIG_FILE": os.devnull, "SOURCE_DATE_EPOCH": "1580601600"},
        timeout_seconds=900, stdout_limit_bytes=2 * 1024 * 1024, stderr_limit_bytes=2 * 1024 * 1024))
    require(result.returncode == 0, "build_command_failed")


def build_release(source_root: Path, output: Path, *, wheelhouse: Path | None, allow_network: bool) -> dict:
    require(wheelhouse is not None or allow_network, "build_network_confirmation_required")
    require(output.is_absolute() and not os.path.lexists(output), "new_absolute_output_required")
    for parent in output.parents:
        plain(parent, directory=True)
    if wheelhouse is not None:
        require(wheelhouse.is_absolute(), "absolute_wheelhouse_required")
        plain(wheelhouse, directory=True)
    source = source_snapshot(source_root)
    validate_documentation_inventory(source)
    project, identity = source_contract(source)
    version = project["version"]
    output.mkdir()  # Never reuse/delete a prior release or environment.
    release_dir = output / "release"
    release_dir.mkdir()
    stage = output / "source"
    stage.mkdir()
    for name, payload in source.items():
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    print(json.dumps({"stage": "build_environment"}), flush=True)
    environment = output / "build-environment"
    command(Path(sys._base_executable), ["-m", "venv", "--without-pip", str(environment)], output)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command(python, ["-m", "ensurepip", "--upgrade"], output)
    source_args = ["--no-index", "--find-links", str(wheelhouse)] if wheelhouse else ["--index-url", "https://pypi.org/simple"]
    command(python, ["-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input", "install", "--no-cache-dir", "--no-deps", "--only-binary=:all:", "--require-hashes", *source_args, "-r", str(stage / "deployment/requirements.build.lock")], output)
    command(python, ["-m", "pip", "--isolated", "check"], output)
    print(json.dumps({"stage": "build_archives"}), flush=True)
    command(python, ["-m", "hatchling", "build", "-t", "wheel", "-t", "sdist", "-d", str(release_dir)], stage)
    require(source_snapshot(source_root) == source and source_snapshot(stage) == source, "source_changed_during_build")
    source_zip = f"Open-Flame-{version}-source.zip"
    with zipfile.ZipFile(release_dir / source_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(source.items()):
            info = zipfile.ZipInfo(f"Open-Flame-{version}-source/{name}", (2020, 2, 2, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED)
    manifest = {"schema_version": 1, "version": version, "scope": "project-only; no third-party runtime binaries", "product_identity": identity,
        "source_zip": source_zip, "source_files": {n: fingerprint(b) for n, b in sorted(source.items())},
        "entrypoint_sha256": {n: hashlib.sha256(source[n]).hexdigest() for n in ENTRYPOINTS},
        "artifacts": {p.name: fingerprint(p.read_bytes()) for p in sorted(release_dir.iterdir())},
        "privacy_check": "explicit inventory, text-only and high-confidence patterns; not a proof of absence of all secrets",
        "authenticity": "unsigned checksums; compare through a trusted channel"}
    (release_dir / "release-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    sums = "".join(f"{manifest['artifacts'][n]['sha256']}  {n}\n" for n in sorted(manifest["artifacts"]))
    sums += f"{hashlib.sha256((release_dir / 'release-manifest.json').read_bytes()).hexdigest()}  release-manifest.json\n"
    (release_dir / "SHA256SUMS").write_text(sums, encoding="ascii", newline="\n")
    verify_release(release_dir)
    return manifest


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ReleaseError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build", help="Create a new local project-only release; never upload.")
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--wheelhouse", type=Path)
    build.add_argument("--allow-network", action="store_true")
    verify = sub.add_parser("verify", help="Read-only integrity and archive contract verification.")
    verify.add_argument("--release-dir", required=True, type=Path)
    sub.add_parser("check-source", help="Read-only source inventory, identity and documentation preflight.")
    try:
        args = parser.parse_args(argv)
        if args.action == "build":
            manifest = build_release(ROOT, args.output, wheelhouse=args.wheelhouse, allow_network=args.allow_network)
        elif args.action == "verify":
            manifest = verify_release(args.release_dir)
        else:
            source = source_snapshot(ROOT)
            validate_documentation_inventory(source)
            project, identity = source_contract(source)
            manifest = {"version": project["version"], "product_identity": identity, "source_files": source}
        print(json.dumps({"status": "passed", "action": args.action, "version": manifest["version"], "product_identity": manifest["product_identity"], "source_files": len(manifest["source_files"])}))
        return 0
    except KeyboardInterrupt:
        print('{"status":"cancelled"}', file=sys.stderr)
        return 130
    except Exception as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_validation_failed"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
