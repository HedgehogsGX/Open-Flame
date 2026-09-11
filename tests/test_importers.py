from __future__ import annotations

from pathlib import Path

import pytest

from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.importers import BatchImportError, parse_batch_file


def settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    return Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
    )


def test_txt_and_headered_csv_import_preserve_auditable_rows() -> None:
    txt = parse_batch_file(
        b"https://youtu.be/abc123\nnot a url\n\n",
        filename="batch.txt",
    )
    assert txt == ["https://youtu.be/abc123", "not a url"]

    csv_values = parse_batch_file(
        (
            "title,url,note\n"
            "one,https://www.youtube.com/watch?v=abc123,ok\n"
            "two,not a url,kept for validation\n"
        ).encode(),
        filename="batch.csv",
    )
    assert csv_values == [
        "https://www.youtube.com/watch?v=abc123",
        "not a url",
    ]


def test_headerless_csv_extracts_urls_and_keeps_invalid_rows() -> None:
    values = parse_batch_file(
        (
            "one,https://x.com/name/status/123456789\n"
            "invalid,row\n"
            "two,https://youtu.be/abc123,https://bilibili.com/video/BV1xx\n"
        ).encode(),
        filename="batch.csv",
    )
    assert values == [
        "https://x.com/name/status/123456789",
        "invalid row",
        "https://youtu.be/abc123",
        "https://bilibili.com/video/BV1xx",
    ]


@pytest.mark.parametrize(
    ("content", "filename", "message"),
    [
        (b"", "empty.txt", "没有"),
        (b"url\n", "batch.xlsx", "仅支持"),
        (b"\xff", "batch.txt", "UTF-8"),
        (b"a\x00b", "batch.txt", "NUL"),
        (b"url,link\na,b\n", "batch.csv", "一个"),
    ],
)
def test_invalid_imports_fail_closed(
    content: bytes, filename: str, message: str
) -> None:
    with pytest.raises(BatchImportError, match=message):
        parse_batch_file(content, filename=filename)


def test_raw_csv_api_creates_batch_without_multipart_dependency(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings(tmp_path)), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(
            "/api/v1/batches/import",
            params={"filename": "urls.csv", "name": "CSV import"},
            content=(
                "url,note\n"
                "https://www.youtube.com/watch?v=abc123,one\n"
                "not a url,invalid but audited\n"
            ).encode(),
            headers={"content-type": "text/csv; charset=utf-8"},
        )
    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "CSV import"
    assert payload["total_count"] == 2
    assert payload["queued_count"] == 1
    assert payload["failed_count"] == 1


def test_import_api_rejects_media_type_and_stream_over_limit(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings(tmp_path)), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        wrong_type = client.post(
            "/api/v1/batches/import",
            params={"filename": "urls.csv"},
            content=b"url\nhttps://youtu.be/test\n",
            headers={"content-type": "application/json"},
        )
        too_large = client.post(
            "/api/v1/batches/import",
            params={"filename": "urls.txt"},
            content=b"x" * (256 * 1024 + 1),
            headers={"content-type": "text/plain"},
        )
    assert wrong_type.status_code == 415
    assert too_large.status_code == 413
