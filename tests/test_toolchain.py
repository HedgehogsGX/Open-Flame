from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import video_download_control.toolchain as toolchain_module
from video_download_control.toolchain import (
    Artifact,
    FfmpegLock,
    LockedFile,
    ToolchainError,
    ToolchainLock,
    VerifiedToolchain,
    YtDlpLock,
    inspect_toolchain,
    install_toolchain,
    load_toolchain_lock,
    verify_toolchain,
)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact(
    *, cache_name: str, content: bytes, install_path: str | None = None
) -> Artifact:
    return Artifact(
        cache_name=cache_name,
        url=f"https://github.com/example/project/releases/download/v1/{cache_name}",
        size=len(content),
        sha256=_digest(content),
        install_path=install_path,
    )


@dataclass(frozen=True)
class _OfflineFixture:
    lock: ToolchainLock
    cache: Path
    payload: dict[str, bytes]


def _write_source_archive(path: Path, license_content: bytes) -> bytes:
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("yt-dlp/LICENSE")
        member.size = len(license_content)
        archive.addfile(member, io.BytesIO(license_content))
    return path.read_bytes()


def _write_ffmpeg_archive(
    path: Path,
    *,
    archive_root: str,
    payload: dict[str, bytes],
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, content in payload.items():
            archive.writestr(f"{archive_root}/{relative}", content)
        for name, content in extra_members:
            archive.writestr(name, content)
    return path.read_bytes()


def _offline_fixture(
    tmp_path: Path,
    *,
    ffmpeg_extra_members: tuple[tuple[str, bytes], ...] = (),
) -> _OfflineFixture:
    cache = (tmp_path / "cache").resolve()
    cache.mkdir()

    yt_dlp = b"offline yt-dlp zipimport fixture\n"
    yt_license = b"offline yt-dlp license fixture\n"
    yt_source_path = cache / "yt-dlp.tar.gz"
    yt_source = _write_source_archive(yt_source_path, yt_license)
    yt_checksums = f"{_digest(yt_dlp)}  yt-dlp\n".encode()
    yt_signature = b"offline signature evidence fixture\n"

    archive_root = "fixture-ffmpeg"
    ffmpeg_license = b"offline FFmpeg LGPL license fixture\n"
    ffmpeg_payload = {
        "bin/ffmpeg.exe": b"offline ffmpeg fixture\n",
        "bin/ffprobe.exe": b"offline ffprobe fixture\n",
    }
    archive_payload = {**ffmpeg_payload, "LICENSE.txt": ffmpeg_license}
    ffmpeg_archive_path = cache / "ffmpeg.zip"
    ffmpeg_archive = _write_ffmpeg_archive(
        ffmpeg_archive_path,
        archive_root=archive_root,
        payload=archive_payload,
        extra_members=ffmpeg_extra_members,
    )

    cached = {
        "yt-dlp": yt_dlp,
        "SHA2-256SUMS": yt_checksums,
        "SHA2-256SUMS.sig": yt_signature,
    }
    for name, content in cached.items():
        (cache / name).write_bytes(content)

    configuration = "--enable-version3 --enable-shared --disable-static"
    lock = ToolchainLock(
        schema_version=1,
        bundle_id="offline-windows-toolchain-fixture",
        target_system=platform.system(),
        target_machines=(platform.machine(),),
        yt_dlp=YtDlpLock(
            version="fixture-yt-dlp",
            execution="python-zipimport",
            entrypoint="yt-dlp/yt-dlp",
            license_expression="Unlicense AND MIT AND ISC",
            artifact=_artifact(cache_name="yt-dlp", content=yt_dlp),
            source_artifact=_artifact(
                cache_name="yt-dlp.tar.gz",
                content=yt_source,
                install_path="evidence/yt-dlp/yt-dlp.tar.gz",
            ),
            checksums_artifact=_artifact(
                cache_name="SHA2-256SUMS",
                content=yt_checksums,
                install_path="evidence/yt-dlp/SHA2-256SUMS",
            ),
            signature_artifact=_artifact(
                cache_name="SHA2-256SUMS.sig",
                content=yt_signature,
                install_path="evidence/yt-dlp/SHA2-256SUMS.sig",
            ),
            license=LockedFile(
                archive_path="yt-dlp/LICENSE",
                install_path="licenses/yt-dlp/LICENSE",
                size=len(yt_license),
                sha256=_digest(yt_license),
            ),
        ),
        ffmpeg=FfmpegLock(
            version="fixture-ffmpeg",
            archive_root=archive_root,
            license_expression="LGPL-3.0-or-later",
            configuration_sha256=_digest(configuration.encode()),
            required_configuration_flags=(
                "--enable-version3",
                "--enable-shared",
                "--disable-static",
            ),
            forbidden_configuration_flags=("--enable-gpl", "--enable-nonfree"),
            artifact=_artifact(cache_name="ffmpeg.zip", content=ffmpeg_archive),
            source_project_url="https://github.com/FFmpeg/FFmpeg",
            build_project_url="https://github.com/BtbN/FFmpeg-Builds",
            payload=tuple(
                LockedFile(
                    archive_path=relative,
                    install_path=f"ffmpeg/{relative}",
                    size=len(content),
                    sha256=_digest(content),
                )
                for relative, content in ffmpeg_payload.items()
            ),
            license=LockedFile(
                archive_path="LICENSE.txt",
                install_path="licenses/ffmpeg/LICENSE.txt",
                size=len(ffmpeg_license),
                sha256=_digest(ffmpeg_license),
            ),
        ),
        network_download_enabled=False,
        isolated_worker_ready=False,
        platform_download_verified=False,
        redistribution_status="blocked_pending_offline_fixture_review",
        raw_bytes=b'{"fixture":"offline-windows-toolchain"}\n',
    )
    return _OfflineFixture(
        lock=lock,
        cache=cache,
        payload={
            lock.yt_dlp.entrypoint: yt_dlp,
            **{
                item.install_path: ffmpeg_payload[item.archive_path or ""]
                for item in lock.ffmpeg.payload
            },
        },
    )


def _install_with_fake_smoke(
    fixture: _OfflineFixture,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke_roots: list[Path] = []

    def fake_smoke(
        tool_root: Path,
        *,
        python_executable: Path | None = None,
        lock: ToolchainLock | None = None,
    ) -> VerifiedToolchain:
        del python_executable
        assert lock == fixture.lock
        assert not target.exists()
        assert tool_root.parent == target.parent
        assert tool_root.name.startswith(".vdc-toolchain-staging-")
        smoke_roots.append(tool_root)
        marker = {
            "schema_version": 1,
            "bundle_id": fixture.lock.bundle_id,
            "status": "passed",
            "streams": ["audio", "video"],
            "network_access": "not_exercised",
        }
        (tool_root / toolchain_module.SMOKE_FILENAME).write_text(
            json.dumps(marker, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return VerifiedToolchain(
            bundle_id=fixture.lock.bundle_id,
            yt_dlp_version=fixture.lock.yt_dlp.version,
            ffmpeg_version=fixture.lock.ffmpeg.version,
            ffprobe_version=fixture.lock.ffmpeg.version,
            offline_smoke_passed=True,
        )

    monkeypatch.setattr(toolchain_module, "run_offline_smoke", fake_smoke)
    result = install_toolchain(
        target,
        artifact_cache=fixture.cache,
        lock=fixture.lock,
    )

    assert len(smoke_roots) == 1
    assert result.state == "ready"
    assert result.detail_code == "ok"


def test_checked_in_lock_is_strictly_pinned_and_fail_closed() -> None:
    lock = load_toolchain_lock()

    assert lock.schema_version == 1
    assert lock.target_system == "Windows"
    assert set(lock.target_machines) == {"AMD64", "x86_64"}
    assert lock.yt_dlp.execution == "python-zipimport"
    assert lock.yt_dlp.license_expression == "Unlicense AND MIT AND ISC"
    assert lock.ffmpeg.license_expression == "LGPL-3.0-or-later"
    assert "--enable-gpl" in lock.ffmpeg.forbidden_configuration_flags
    assert "--enable-nonfree" in lock.ffmpeg.forbidden_configuration_flags
    assert "/latest/" not in lock.yt_dlp.artifact.url
    assert "/latest/" not in lock.ffmpeg.artifact.url
    assert lock.network_download_enabled is False
    assert lock.isolated_worker_ready is False
    assert lock.platform_download_verified is False
    assert lock.redistribution_status.startswith("blocked_")


def test_package_managed_lock_may_be_hardlinked_by_a_wheel_installer(
    tmp_path: Path,
) -> None:
    cached = tmp_path / "cached-lock.json"
    installed = tmp_path / "installed-lock.json"
    cached.write_bytes(toolchain_module.LOCK_PATH.read_bytes())
    os.link(cached, installed)

    lock = load_toolchain_lock(installed)

    assert cached.stat().st_nlink >= 2
    assert lock.bundle_id.startswith("windows-x64-")


@pytest.mark.parametrize(
    "mutation",
    [
        "yt-execution",
        "yt-license",
        "ffmpeg-license",
        "enable-network",
        "claim-isolation",
        "claim-platform",
        "claim-redistribution",
        "latest-url",
        "duplicate-payload",
    ],
)
def test_lock_parser_rejects_unapproved_license_and_policy_mutations(
    tmp_path: Path, mutation: str
) -> None:
    document = json.loads(toolchain_module.LOCK_PATH.read_text(encoding="utf-8"))
    if mutation == "yt-execution":
        document["yt_dlp"]["execution"] = "pyinstaller"
    elif mutation == "yt-license":
        document["yt_dlp"]["license_expression"] = "Unlicense"
    elif mutation == "ffmpeg-license":
        document["ffmpeg"]["license_expression"] = "GPL-3.0-or-later"
    elif mutation == "enable-network":
        document["policy"]["network_download_enabled"] = True
    elif mutation == "claim-isolation":
        document["policy"]["isolated_worker_ready"] = True
    elif mutation == "claim-platform":
        document["policy"]["platform_download_verified"] = True
    elif mutation == "claim-redistribution":
        document["policy"]["redistribution_status"] = (
            "approved_for_public_redistribution"
        )
    elif mutation == "latest-url":
        document["yt_dlp"]["artifact"]["url"] = (
            "https://github.com/yt-dlp/yt-dlp/releases/download/latest/yt-dlp"
        )
    elif mutation == "duplicate-payload":
        document["ffmpeg"]["payload"][1]["install_path"] = document["ffmpeg"][
            "payload"
        ][0]["install_path"]
    else:  # pragma: no cover - parametrization is intentionally exhaustive
        raise AssertionError(mutation)
    path = tmp_path / "mutated-lock.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ToolchainError) as captured:
        load_toolchain_lock(path)

    assert captured.value.code == "lock_schema_invalid"


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "C:/outside-staging",
        "D:outside-staging",
        "evidence/file:alternate-stream",
        "CON",
        "licenses/COM1.txt",
        "licenses/name.",
        "licenses/name ",
        "licenses/./normalized-away",
        "licenses//normalized-away",
    ],
)
def test_lock_parser_rejects_windows_unsafe_relative_paths(
    tmp_path: Path, unsafe_path: str
) -> None:
    document = json.loads(toolchain_module.LOCK_PATH.read_text(encoding="utf-8"))
    document["yt_dlp"]["entrypoint"] = unsafe_path
    path = tmp_path / "unsafe-path-lock.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ToolchainError) as captured:
        load_toolchain_lock(path)

    assert captured.value.code == "lock_schema_invalid"


def test_lock_parser_rejects_windows_unsafe_cache_names(tmp_path: Path) -> None:
    document = json.loads(toolchain_module.LOCK_PATH.read_text(encoding="utf-8"))
    document["yt_dlp"]["artifact"]["cache_name"] = "yt-dlp:alternate-stream"
    path = tmp_path / "unsafe-cache-lock.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ToolchainError) as captured:
        load_toolchain_lock(path)

    assert captured.value.code == "lock_schema_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows namespace semantics")
@pytest.mark.parametrize(
    "unsafe_root",
    [
        Path(r"\\server\share\vdc-tools"),
        Path(r"\\?\C:\vdc-tools"),
        Path(r"\\.\C:\vdc-tools"),
    ],
    ids=["unc", "extended-device", "dos-device"],
)
def test_install_rejects_unc_and_device_namespace_roots(
    tmp_path: Path, unsafe_root: Path
) -> None:
    fixture = _offline_fixture(tmp_path)

    with pytest.raises(ToolchainError) as captured:
        install_toolchain(unsafe_root, artifact_cache=fixture.cache, lock=fixture.lock)

    assert captured.value.code == "path_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
@pytest.mark.parametrize("suffix", ["tools:stream", "CON", "tools.", "tools "])
def test_install_rejects_windows_unsafe_root_segments(
    tmp_path: Path, suffix: str
) -> None:
    fixture = _offline_fixture(tmp_path)
    unsafe_root = tmp_path.resolve() / suffix

    with pytest.raises(ToolchainError) as captured:
        install_toolchain(unsafe_root, artifact_cache=fixture.cache, lock=fixture.lock)

    assert captured.value.code == "path_invalid"


@pytest.mark.parametrize(
    "redirect_url",
    [
        "http://release-assets.githubusercontent.com/artifact",
        "https://127.0.0.1/private",
        "https://user@github.com/artifact",
        "https://github.com:444/artifact",
    ],
)
def test_redirect_handler_rejects_an_unsafe_next_hop_before_following(
    redirect_url: str,
) -> None:
    request = toolchain_module.urllib.request.Request(
        "https://github.com/example/project/releases/download/v1/artifact"
    )
    handler = toolchain_module._ValidatedRedirectHandler()

    with pytest.raises(ToolchainError) as captured:
        handler.redirect_request(request, None, 302, "Found", {}, redirect_url)

    assert captured.value.code == "download_redirect_rejected"


def test_redirect_handler_accepts_a_pinned_https_asset_host() -> None:
    request = toolchain_module.urllib.request.Request(
        "https://github.com/example/project/releases/download/v1/artifact"
    )
    handler = toolchain_module._ValidatedRedirectHandler()
    redirect_url = (
        "https://release-assets.githubusercontent.com:443/artifact?signature=fixed"
    )

    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, redirect_url
    )

    assert redirected is not None
    assert redirected.full_url == redirect_url


def test_download_enforces_a_total_wall_clock_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]

    class _Response:
        headers = {"Content-Length": "1"}
        fp = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self) -> str:
            return "https://release-assets.githubusercontent.com/artifact"

        def read1(self, _size: int) -> bytes:
            now[0] = 2.0
            return b"x"

    class _Opener:
        def open(self, _request, *, timeout: float):
            assert timeout == 1.0
            return _Response()

    monkeypatch.setattr(toolchain_module, "DOWNLOAD_TOTAL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(toolchain_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        toolchain_module.urllib.request,
        "build_opener",
        lambda *_handlers: _Opener(),
    )
    artifact = _artifact(cache_name="artifact", content=b"x")

    with pytest.raises(ToolchainError) as captured:
        toolchain_module._download_artifact(artifact, tmp_path / "artifact")

    assert captured.value.code == "download_timeout"


def test_none_tool_root_reports_unconfigured_without_claiming_capabilities(
    tmp_path: Path,
) -> None:
    fixture = _offline_fixture(tmp_path)

    status = inspect_toolchain(None, lock=fixture.lock)

    assert status.to_dict() == {
        "state": "unconfigured",
        "detail_code": "tool_root_unconfigured",
        "yt_dlp_version": None,
        "ffmpeg_version": None,
        "ffprobe_version": None,
        "offline_smoke_passed": False,
        "isolated_worker_ready": False,
        "platform_download_verified": False,
        "redistribution_status": "blocked_pending_offline_fixture_review",
        "network_download_enabled": False,
    }


def test_forged_cache_install_is_atomic_and_yields_a_complete_verified_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _offline_fixture(tmp_path)
    target = (tmp_path / "managed-tools").resolve()

    _install_with_fake_smoke(fixture, target, monkeypatch)

    verified = verify_toolchain(
        target,
        execute_version_checks=False,
        lock=fixture.lock,
    )
    assert verified.offline_smoke_passed is True
    assert (target / toolchain_module.LOCK_COPY_FILENAME).read_bytes() == (
        fixture.lock.raw_bytes
    )
    for relative, content in fixture.payload.items():
        assert (target / relative).read_bytes() == content
    assert not list(tmp_path.glob(".vdc-toolchain-staging-*"))


def test_cached_artifact_hash_tampering_fails_before_target_is_created(
    tmp_path: Path,
) -> None:
    fixture = _offline_fixture(tmp_path)
    target = (tmp_path / "managed-tools").resolve()
    corrupted = bytearray((fixture.cache / "yt-dlp").read_bytes())
    corrupted[0] ^= 1
    (fixture.cache / "yt-dlp").write_bytes(corrupted)

    with pytest.raises(ToolchainError) as captured:
        install_toolchain(target, artifact_cache=fixture.cache, lock=fixture.lock)

    assert captured.value.code == "artifact_cache_invalid"
    assert not target.exists()
    assert not list(tmp_path.glob(".vdc-toolchain-staging-*"))


@pytest.mark.parametrize(
    "extra_members",
    [
        (("../escape.exe", b"escape"),),
        (("fixture-ffmpeg/BIN/FFMPEG.EXE", b"case-fold duplicate"),),
    ],
    ids=["unsafe-path", "duplicate-case-folded-path"],
)
def test_unsafe_or_duplicate_ffmpeg_zip_fails_atomically(
    tmp_path: Path,
    extra_members: tuple[tuple[str, bytes], ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _offline_fixture(tmp_path, ffmpeg_extra_members=extra_members)
    target = (tmp_path / "managed-tools").resolve()
    monkeypatch.setattr(
        toolchain_module,
        "run_offline_smoke",
        lambda *_args, **_kwargs: pytest.fail("smoke must not run for invalid ZIP"),
    )

    with pytest.raises(ToolchainError) as captured:
        install_toolchain(target, artifact_cache=fixture.cache, lock=fixture.lock)

    assert captured.value.code == "archive_invalid"
    assert not target.exists()
    assert not (tmp_path / "escape.exe").exists()
    assert not list(tmp_path.glob(".vdc-toolchain-staging-*"))


def test_installed_hash_tampering_and_extra_file_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _offline_fixture(tmp_path)
    target = (tmp_path / "managed-tools").resolve()
    _install_with_fake_smoke(fixture, target, monkeypatch)

    ffprobe = target / "ffmpeg/bin/ffprobe.exe"
    corrupted = bytearray(ffprobe.read_bytes())
    corrupted[-1] ^= 1
    ffprobe.write_bytes(corrupted)
    with pytest.raises(ToolchainError) as captured:
        verify_toolchain(target, execute_version_checks=False, lock=fixture.lock)
    assert captured.value.code == "bundle_invalid"

    ffprobe.write_bytes(fixture.payload["ffmpeg/bin/ffprobe.exe"])
    (target / "unexpected-private-marker.txt").write_text(
        "unexpected", encoding="utf-8"
    )
    with pytest.raises(ToolchainError) as captured:
        verify_toolchain(target, execute_version_checks=False, lock=fixture.lock)
    assert captured.value.code == "bundle_invalid"


def test_install_refuses_to_overwrite_an_existing_target(
    tmp_path: Path,
) -> None:
    fixture = _offline_fixture(tmp_path)
    target = (tmp_path / "managed-tools").resolve()
    target.mkdir()
    sentinel = target / "user-owned.txt"
    sentinel.write_text("preserve me", encoding="utf-8")

    with pytest.raises(ToolchainError) as captured:
        install_toolchain(target, artifact_cache=fixture.cache, lock=fixture.lock)

    assert captured.value.code == "target_exists"
    assert sentinel.read_text(encoding="utf-8") == "preserve me"
    assert set(target.iterdir()) == {sentinel}
    assert not list(tmp_path.glob(".vdc-toolchain-staging-*"))
