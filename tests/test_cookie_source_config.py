from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from video_download_control.cookie_source_config import (
    CookieSourceConfigError,
    load_cookie_source_config,
    revalidate_cookie_source_config,
)
from video_download_control.domain import Platform


def _write_config(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o444)


def _document(*entries: dict[str, str]) -> dict[str, object]:
    return {"schema_version": 1, "cookie_sources": list(entries)}


def _entry(platform: str, credential_ref: str, path: Path) -> dict[str, str]:
    return {
        "platform": platform,
        "credential_ref": credential_ref,
        "path": str(path),
    }


def test_cookie_source_config_loads_sorted_strict_private_mapping(
    tmp_path: Path,
) -> None:
    youtube = (tmp_path / "youtube.cookies.txt").resolve()
    douyin = (tmp_path / "douyin.cookies.txt").resolve()
    config_path = (tmp_path / "cookie-sources.json").resolve()
    _write_config(
        config_path,
        _document(
            _entry("youtube", "youtube-profile", youtube),
            _entry("douyin", "douyin-profile", douyin),
        ),
    )

    loaded = load_cookie_source_config(config_path)

    assert [source.platform for source in loaded.sources] == [
        Platform.DOUYIN,
        Platform.YOUTUBE,
    ]
    assert [source.credential_ref for source in loaded.sources] == [
        "douyin-profile",
        "youtube-profile",
    ]
    assert loaded.path == config_path
    assert loaded.size == config_path.stat().st_size
    assert len(loaded.sha256) == 64
    assert "profile" not in repr(loaded)
    assert str(tmp_path) not in repr(loaded)
    revalidate_cookie_source_config(loaded)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"schema_version": 2, "cookie_sources": []},
        {"schema_version": True, "cookie_sources": []},
        {"schema_version": 1, "cookie_sources": {}, "extra": []},
        _document({"platform": "youtube", "credential_ref": "ref"}),
        _document(
            {"platform": "unknown", "credential_ref": "ref", "path": "C:\\x"}
        ),
    ],
)
def test_cookie_source_config_rejects_malformed_documents_without_details(
    tmp_path: Path,
    document: object,
) -> None:
    config_path = (tmp_path / "private-marker.json").resolve()
    _write_config(config_path, document)

    with pytest.raises(CookieSourceConfigError) as exc_info:
        load_cookie_source_config(config_path)

    assert str(exc_info.value) == "cookie source configuration is invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "private-marker" not in str(exc_info.value)


def test_cookie_source_config_rejects_duplicate_keys_platforms_and_self_reference(
    tmp_path: Path,
) -> None:
    config_path = (tmp_path / "cookie-sources.json").resolve()
    config_path.write_text(
        '{"schema_version":1,"schema_version":1,"cookie_sources":[]}',
        encoding="utf-8",
    )
    config_path.chmod(0o444)
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(config_path)

    config_path.chmod(0o666)
    _write_config(
        config_path,
        _document(
            _entry("youtube", "one", (tmp_path / "one.txt").resolve()),
            _entry("youtube", "two", (tmp_path / "two.txt").resolve()),
        ),
    )
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(config_path)

    config_path.chmod(0o666)
    _write_config(config_path, _document(_entry("youtube", "self", config_path)))
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(config_path)


def test_cookie_source_config_rejects_writable_empty_oversized_and_linked_files(
    tmp_path: Path,
) -> None:
    writable = (tmp_path / "writable.json").resolve()
    writable.write_text(json.dumps(_document()), encoding="utf-8")
    writable.chmod(0o666)
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(writable)

    empty = (tmp_path / "empty.json").resolve()
    empty.write_bytes(b"")
    empty.chmod(0o444)
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(empty)

    oversized = (tmp_path / "oversized.json").resolve()
    oversized.write_bytes(b"x" * 65)
    oversized.chmod(0o444)
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(oversized, max_bytes=64)

    original = (tmp_path / "original.json").resolve()
    _write_config(original, _document())
    hardlink = (tmp_path / "hardlink.json").resolve()
    try:
        os.link(original, hardlink)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {exc}")
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(original)
    with pytest.raises(CookieSourceConfigError):
        load_cookie_source_config(hardlink)


def test_cookie_source_config_revalidation_detects_replacement_or_content_change(
    tmp_path: Path,
) -> None:
    config_path = (tmp_path / "cookie-sources.json").resolve()
    _write_config(config_path, _document())
    loaded = load_cookie_source_config(config_path)

    config_path.chmod(0o666)
    config_path.write_text(
        json.dumps(
            _document(
                _entry(
                    "youtube",
                    "changed-private-ref",
                    (tmp_path / "changed.cookies.txt").resolve(),
                )
            )
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o444)

    with pytest.raises(CookieSourceConfigError) as exc_info:
        revalidate_cookie_source_config(loaded)

    assert str(exc_info.value) == "cookie source configuration is invalid"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert "changed" not in str(exc_info.value)
