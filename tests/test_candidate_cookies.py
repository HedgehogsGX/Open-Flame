from __future__ import annotations

import os
from pathlib import Path

import pytest

import video_download_control.candidate_cookies as cookie_module
from video_download_control.adapters.base import AdapterContext
from video_download_control.candidate_cookies import (
    AttemptCookieResolver,
    CookiePreparationError,
    CookieSource,
)
from video_download_control.domain import Platform


def make_context(tmp_path: Path) -> AdapterContext:
    attempt = (tmp_path / "attempt").resolve()
    attempt.mkdir()
    return AdapterContext(
        worker_id="candidate-test",
        attempt_id="attempt-1",
        temporary_dir=attempt,
    )


def make_read_only_cookie(tmp_path: Path, content: bytes = b"cookie-value") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = (tmp_path / "mounted-cookie.txt").resolve()
    source.write_bytes(content)
    source.chmod(0o444)
    return source


def restore_writable(*paths: Path) -> None:
    for path in paths:
        try:
            path.chmod(0o600)
        except OSError:
            pass


def test_resolver_copies_to_fresh_private_fsynced_attempt_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_read_only_cookie(tmp_path)
    context = make_context(tmp_path)
    resolver = AttemptCookieResolver(
        (
            CookieSource(
                platform=Platform.YOUTUBE,
                credential_ref="youtube-primary",
                path=source,
            ),
        ),
        max_cookie_bytes=4096,
    )
    fsynced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(cookie_module.os, "fsync", recording_fsync)
    try:
        resolver.validate_sources()
        first = resolver(Platform.YOUTUBE, "youtube-primary", context)
        second = resolver(Platform.YOUTUBE, "youtube-primary", context)
    finally:
        restore_writable(source)

    assert first.path != source
    assert first.path != second.path
    assert first.path.parent == context.temporary_dir / "secrets"
    assert first.path.read_bytes() == b"cookie-value"
    assert second.path.read_bytes() == b"cookie-value"
    assert first.path.stat().st_nlink == 1
    if os.name == "posix":
        assert first.path.stat().st_mode & 0o777 == 0o600
        assert first.path.parent.stat().st_mode & 0o777 == 0o700
    assert len(fsynced) >= 2


def test_resolver_rejects_writable_source_without_leaking_values(
    tmp_path: Path,
) -> None:
    secret_content = "cookie-secret-do-not-log"
    credential_ref = "private-profile"
    source = (tmp_path / "sensitive-name.txt").resolve()
    source.write_text(secret_content, encoding="utf-8")
    resolver = AttemptCookieResolver(
        (CookieSource(Platform.X, credential_ref, source),)
    )

    with pytest.raises(CookiePreparationError) as caught:
        resolver.validate_sources()

    diagnostic = str(caught.value)
    assert secret_content not in diagnostic
    assert credential_ref not in diagnostic
    assert str(source) not in diagnostic
    assert "sensitive-name" not in diagnostic


def test_public_cookie_error_discards_path_from_entire_exception_chain(
    tmp_path: Path,
) -> None:
    marker = "private-cookie-path-marker"
    missing = (tmp_path / marker / "cookies.txt").resolve()
    resolver = AttemptCookieResolver(
        (CookieSource(Platform.YOUTUBE, "private-ref", missing),)
    )

    with pytest.raises(CookiePreparationError) as caught:
        resolver.validate_sources()

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_resolver_rejects_symlink_and_hardlink_sources(tmp_path: Path) -> None:
    target = make_read_only_cookie(tmp_path)
    linked = (tmp_path / "linked-cookie.txt").resolve()
    try:
        os.link(target, linked)
    except OSError:
        restore_writable(target)
        pytest.skip("hard links are unavailable on this filesystem")
    try:
        resolver = AttemptCookieResolver(
            (CookieSource(Platform.YOUTUBE, "profile", target),)
        )
        with pytest.raises(CookiePreparationError):
            resolver.validate_sources()
    finally:
        restore_writable(target, linked)

    symlink_target = make_read_only_cookie(tmp_path / "symlink-case")
    symlink = symlink_target.parent / "cookie-link.txt"
    try:
        symlink.symlink_to(symlink_target)
    except OSError:
        restore_writable(symlink_target)
        pytest.skip("symlinks are unavailable on this filesystem")
    try:
        resolver = AttemptCookieResolver(
            (CookieSource(Platform.YOUTUBE, "profile", symlink.resolve(strict=False)),)
        )
        # Passing resolve() would hide the link, so construct the explicit
        # absolute spelling that includes the symlink leaf.
        resolver = AttemptCookieResolver(
            (CookieSource(Platform.YOUTUBE, "profile", symlink.absolute()),)
        )
        with pytest.raises(CookiePreparationError):
            resolver.validate_sources()
    finally:
        restore_writable(symlink_target)


def test_resolver_detects_source_path_swap_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_read_only_cookie(tmp_path, b"original-cookie-secret")
    original = source.with_name("original-preserved.txt")
    context = make_context(tmp_path)
    credential_ref = "swap-profile"
    resolver = AttemptCookieResolver(
        (CookieSource(Platform.YOUTUBE, credential_ref, source),)
    )
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.replace(original)
            source.write_bytes(b"replacement-cookie-secret")
            source.chmod(0o444)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(cookie_module.os, "open", swapping_open)
    try:
        with pytest.raises(CookiePreparationError) as caught:
            resolver(Platform.YOUTUBE, credential_ref, context)
    finally:
        restore_writable(source, original)

    assert swapped is True
    diagnostic = str(caught.value)
    assert credential_ref not in diagnostic
    assert "original-cookie-secret" not in diagnostic
    assert "replacement-cookie-secret" not in diagnostic
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert not (context.temporary_dir / "secrets").exists()


def test_resolver_detects_source_path_swap_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_read_only_cookie(tmp_path, b"stable-cookie")
    original = source.with_name("opened-source.txt")
    context = make_context(tmp_path)
    resolver = AttemptCookieResolver(
        (CookieSource(Platform.YOUTUBE, "copy-swap", source),)
    )
    real_read = os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = real_read(descriptor, size)
        if chunk and not swapped:
            swapped = True
            source.replace(original)
            source.write_bytes(b"replacement")
            source.chmod(0o444)
        return chunk

    monkeypatch.setattr(cookie_module.os, "read", swapping_read)
    try:
        with pytest.raises(CookiePreparationError):
            resolver(Platform.YOUTUBE, "copy-swap", context)
    finally:
        restore_writable(source, original)

    assert swapped is True
    assert list((context.temporary_dir / "secrets").glob("*.cookies.txt")) == []


def test_wrong_platform_or_reference_creates_no_attempt_secret(
    tmp_path: Path,
) -> None:
    source = make_read_only_cookie(tmp_path)
    context = make_context(tmp_path)
    resolver = AttemptCookieResolver(
        (CookieSource(Platform.YOUTUBE, "expected-ref", source),)
    )
    try:
        for platform, credential_ref in (
            (Platform.X, "expected-ref"),
            (Platform.YOUTUBE, "wrong-ref"),
        ):
            with pytest.raises(CookiePreparationError) as caught:
                resolver(platform, credential_ref, context)
            assert credential_ref not in str(caught.value)
    finally:
        restore_writable(source)

    assert not (context.temporary_dir / "secrets").exists()


def test_cookie_configuration_repr_hides_path_and_reference(tmp_path: Path) -> None:
    source = (tmp_path / "private-source-name.txt").resolve()
    configured = CookieSource(Platform.BILIBILI, "hidden-reference", source)
    resolver = AttemptCookieResolver((configured,))

    assert "hidden-reference" not in repr(configured)
    assert str(source) not in repr(configured)
    assert "hidden-reference" not in repr(resolver)
    assert str(source) not in repr(resolver)
    assert resolver.configured_platforms == (Platform.BILIBILI,)


def test_cookie_source_rejects_relative_path_and_duplicate_platform(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        CookieSource(Platform.X, "profile", Path("relative-cookie.txt"))
    with pytest.raises(ValueError, match="normalized"):
        CookieSource(
            Platform.X,
            "profile",
            tmp_path.resolve() / "nested" / ".." / "cookie.txt",
        )

    first = CookieSource(Platform.X, "first", (tmp_path / "one").resolve())
    second = CookieSource(Platform.X, "second", (tmp_path / "two").resolve())
    with pytest.raises(ValueError, match="one cookie source"):
        AttemptCookieResolver((first, second))


@pytest.mark.skipif(os.name != "posix", reason="directory-fd cleanup is POSIX-only")
def test_cleanup_uses_attested_directory_fd_and_preserves_replacement(
    tmp_path: Path,
) -> None:
    live_directory = tmp_path / "secrets"
    live_directory.mkdir(mode=0o700)
    filename = "youtube-owned.cookies.txt"
    owned_target = live_directory / filename
    owned_target.write_bytes(b"owned-copy")
    owned_target.chmod(0o600)
    opened_directory = os.open(
        live_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    identity = (owned_target.stat().st_dev, owned_target.stat().st_ino)
    displaced_directory = tmp_path / "displaced-secrets"
    try:
        live_directory.replace(displaced_directory)
        live_directory.mkdir(mode=0o700)
        replacement = live_directory / filename
        replacement.write_bytes(b"replacement-must-survive")
        replacement.chmod(0o600)

        AttemptCookieResolver._remove_owned_target_at(
            opened_directory,
            filename,
            identity,
        )

        assert not (displaced_directory / filename).exists()
        assert replacement.read_bytes() == b"replacement-must-survive"
    finally:
        os.close(opened_directory)
