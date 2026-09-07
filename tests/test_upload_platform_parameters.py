from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import struct
import time
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from video_download_control.api import create_app
from video_download_control.uploads import service as upload_service_module
from video_download_control.uploads.bridge import install_statement_policy
from video_download_control.uploads.contracts import BackendResult, UploadError
from video_download_control.uploads.service import (
    SCHEDULE_LEAD_SECONDS,
    UploadService,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.uploads = []
        self.cover_payloads = []

    def inspect(self) -> dict:
        return {"ready": True, "code": "synthetic_ready"}

    def login(self, platform, account_id, stop) -> BackendResult:
        return BackendResult("ready", "synthetic_ready")

    check = login

    def upload(self, request, stop) -> BackendResult:
        self.cover_payloads.append((
            request.cover_landscape_path.read_bytes()
            if request.cover_landscape_path is not None else None,
            request.cover_portrait_path.read_bytes()
            if request.cover_portrait_path is not None else None,
        ))
        self.uploads.append(request)
        return BackendResult(
            "draft_saved" if request.mode == "draft" else "submitted",
            "synthetic_result",
        )


@pytest.fixture
def upload_service(tmp_path):
    backend = RecordingBackend()
    service = UploadService(tmp_path / "uploads", backend)
    service.start()
    yield service
    service.stop()


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("synthetic upload operation did not complete")


def _ready_account(service: UploadService, platform: str) -> dict:
    account = service.add_account(platform, f"{platform}-{uuid4().hex[:8]}")
    with sqlite3.connect(service.database_path) as database:
        database.execute(
            "UPDATE accounts SET auth_state='ready',code='synthetic_ready' WHERE id=?",
            (account["id"],),
        )
    return account


def _source(service: UploadService, tmp_path: Path) -> dict:
    path = tmp_path / "platform-parameters.mp4"
    path.write_bytes(b"synthetic platform parameter video")
    return service.import_source(path, path.name)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int, rgb: tuple[int, int, int] = (32, 96, 192)) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    pixels = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(pixels))
        + _png_chunk(b"IEND", b"")
    )


def _cover(
    service: UploadService,
    tmp_path: Path,
    name: str,
    width: int,
    height: int,
) -> tuple[dict, bytes]:
    payload = _png(width, height)
    path = tmp_path / name
    path.write_bytes(payload)
    return service.import_cover(path, name), payload


def _scheduled_at(platform: str, *, offset_minutes: int = 570) -> int:
    candidate = int(time.time()) + SCHEDULE_LEAD_SECONDS[platform] + 3600
    if platform == "tencent":
        return ((candidate + offset_minutes * 60 + 3599) // 3600 * 3600
                - offset_minutes * 60)
    return (candidate // 60 + 1) * 60


def _create_jobs(
    service: UploadService,
    source_id: str,
    account_ids: list[str],
    *,
    key: str,
    target_overrides: list[dict] | None = None,
) -> list[dict]:
    return service.create_jobs(
        source_id=source_id,
        account_ids=account_ids,
        title="公共标题",
        description="公共说明",
        tags=["公共标签"],
        category_id=249,
        copyright=1,
        idempotency_key=key,
        target_overrides=target_overrides,
    )


def test_one_batch_keeps_platform_metadata_separate_and_idempotency_order_independent(
    upload_service, tmp_path
):
    accounts = {
        platform: _ready_account(upload_service, platform)
        for platform in ("bilibili", "douyin", "tencent")
    }
    source = _source(upload_service, tmp_path)
    overrides = [
        {
            "account_id": accounts["bilibili"]["id"],
            "title": "B站独立标题",
            "tags": ["B站", "知识"],
            "platform_options": {"dynamic": "B站动态"},
        },
        {
            "account_id": accounts["douyin"]["id"],
            "title": "抖音独立标题",
            "tags": ["抖音"],
            "platform_options": {"declaration": "内容为个人观点或见解"},
        },
        {
            "account_id": accounts["tencent"]["id"],
            "title": "视频号独立标题",
            "tags": ["视频号", "本地工具"],
            "platform_options": {"short_title": "视频号短标题测试"},
        },
    ]
    account_ids = [accounts[p]["id"] for p in ("bilibili", "douyin", "tencent")]

    jobs = _create_jobs(
        upload_service,
        source["id"],
        account_ids,
        key="platform_batch_order",
        target_overrides=overrides,
    )
    by_platform = {job["platform"]: job for job in jobs}
    assert (by_platform["bilibili"]["title"], by_platform["bilibili"]["tags"]) == (
        "B站独立标题",
        ["B站", "知识"],
    )
    assert (by_platform["douyin"]["title"], by_platform["douyin"]["tags"]) == (
        "抖音独立标题",
        ["抖音"],
    )
    assert (by_platform["tencent"]["title"], by_platform["tencent"]["tags"]) == (
        "视频号独立标题",
        ["视频号", "本地工具"],
    )

    replay = _create_jobs(
        upload_service,
        source["id"],
        list(reversed(account_ids)),
        key="platform_batch_order",
        target_overrides=list(reversed(overrides)),
    )
    assert [job["id"] for job in replay] == [job["id"] for job in jobs]


def test_cross_platform_options_and_duplicate_overrides_are_rejected(upload_service, tmp_path):
    account = _ready_account(upload_service, "bilibili")
    second_account = _ready_account(upload_service, "douyin")
    source = _source(upload_service, tmp_path)

    with pytest.raises(UploadError, match="^unsupported_platform_option$"):
        _create_jobs(
            upload_service,
            source["id"],
            [account["id"]],
            key="cross_platform_option",
            target_overrides=[{
                "account_id": account["id"],
                "platform_options": {"declaration": "内容由AI生成"},
            }],
        )

    for invalid_tag in ("#travel", "旅行＃", "one，two"):
        with pytest.raises(UploadError, match="^invalid_tags$"):
            _create_jobs(
                upload_service,
                source["id"],
                [account["id"]],
                key="invalid_tag_" + uuid4().hex,
                target_overrides=[{
                    "account_id": account["id"], "tags": [invalid_tag],
                }],
            )

    with pytest.raises(UploadError, match="^invalid_target_override$"):
        _create_jobs(
            upload_service,
            source["id"],
            [account["id"], second_account["id"]],
            key="duplicate_overrides",
            target_overrides=[
                {"account_id": account["id"], "title": "第一次"},
                {"account_id": account["id"], "title": "第二次"},
            ],
        )


@pytest.mark.parametrize(
    "bilibili_only",
    [
        {"category_id": 21},
        {"copyright": 2},
        {"source_credit": "explicit source"},
        {"category_id": None},
    ],
)
def test_non_bilibili_target_override_rejects_bilibili_only_fields(
    upload_service, tmp_path, bilibili_only
):
    account = _ready_account(upload_service, "douyin")
    source = _source(upload_service, tmp_path)
    key = "wrong_platform_field_" + uuid4().hex

    with pytest.raises(UploadError, match="^unsupported_platform_field$"):
        _create_jobs(
            upload_service,
            source["id"],
            [account["id"]],
            key=key,
            target_overrides=[{"account_id": account["id"], **bilibili_only}],
        )

    assert upload_service.jobs() == []
    created = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key=key,
        target_overrides=[{"account_id": account["id"], "title": "有效抖音标题"}],
    )
    assert created[0]["title"] == "有效抖音标题"


def test_non_bilibili_batch_keeps_legacy_shared_defaults_but_rejects_target_fields(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "douyin")
    source = _source(upload_service, tmp_path)
    key = "shared_bilibili_fields"

    valid = upload_service.create_jobs(
        source_id=source["id"], account_ids=[account["id"]], title="抖音标题",
        description="", tags=["抖音"], category_id=None, copyright=1,
        source_credit="", idempotency_key=key,
    )
    replay = upload_service.create_jobs(
        source_id=source["id"], account_ids=[account["id"]], title="抖音标题",
        description="", tags=["抖音"], category_id=12, copyright=2,
        source_credit="changed", idempotency_key=key,
    )
    assert replay[0]["id"] == valid[0]["id"]
    assert upload_service.create_jobs(
        source_id=source["id"], account_ids=[account["id"]], title="抖音标题",
        description="", tags=["抖音"], category_id=None, copyright=None,
        source_credit="", idempotency_key=key,
    )[0]["id"] == valid[0]["id"]

    compatibility_key = "legacy_shared_defaults"
    created = upload_service.create_jobs(
        source_id=source["id"], account_ids=[account["id"]], title="抖音标题",
        description="", tags=["抖音"], category_id=12, copyright=1,
        source_credit="legacy ignored", idempotency_key=compatibility_key,
    )
    assert created[0]["category_id"] is None
    assert created[0]["copyright"] == 1
    assert created[0]["source_credit"] == ""
    assert created[0]["state"] == "draft"


def test_bilibili_original_rejects_source_credit_without_poisoning_idempotency(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "bilibili")
    source = _source(upload_service, tmp_path)
    key = "original_source_credit"

    with pytest.raises(UploadError, match="^source_credit_not_allowed$"):
        upload_service.create_jobs(
            source_id=source["id"], account_ids=[account["id"]], title="原创标题",
            description="", tags=["原创"], category_id=21, copyright=1,
            source_credit="不应静默忽略", idempotency_key=key,
        )
    created = upload_service.create_jobs(
        source_id=source["id"], account_ids=[account["id"]], title="原创标题",
        description="", tags=["原创"], category_id=21, copyright=1,
        source_credit="", idempotency_key=key,
    )
    assert created[0]["source_credit"] == ""


def test_cover_import_content_response_and_tamper_rejection(settings):
    backend = RecordingBackend()
    app = create_app(settings)
    app.state.upload_service_factory = lambda root: UploadService(root, backend)
    payload = _png(400, 300)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/v1/uploads/session").json()["csrf_token"]
        client.headers["X-Upload-CSRF"] = token
        response = client.post(
            "/api/v1/uploads/covers?name=cover.png",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 201, response.text
        cover = response.json()
        assert cover["mime_type"] == "image/png"
        assert (cover["width"], cover["height"]) == (400, 300)
        assert cover["sha256"] == hashlib.sha256(payload).hexdigest()
        assert client.get(
            f"/api/v1/uploads/covers/resolve?ids={cover['id']}"
        ).json() == [cover]

        content = client.get(f"/api/v1/uploads/covers/{cover['id']}/content")
        assert content.status_code == 200
        assert content.headers["content-type"] == "image/png"
        assert content.content == payload

        stored = app.state.upload_manager.root / "assets" / f"{cover['id']}.png"
        stored.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        changed = client.get(f"/api/v1/uploads/covers/{cover['id']}/content")
        assert changed.status_code == 409
        assert changed.json() == {"detail": "cover_changed"}
        deletion = client.delete(f"/api/v1/uploads/covers/{cover['id']}/media")
        assert deletion.status_code == 409
        assert deletion.json() == {"detail": "cover_changed"}
        assert stored.exists()


def test_cover_history_is_cursor_paginated_without_duplicates(upload_service, tmp_path):
    imported = [
        _cover(upload_service, tmp_path, f"cover-{index}.png", 400, 300)[0]
        for index in range(3)
    ]

    first = upload_service.cover_page(limit=2)
    second = upload_service.cover_page(cursor=first["next_cursor"], limit=2)

    assert [item["id"] for item in first["items"]] == [
        imported[2]["id"], imported[1]["id"]
    ]
    assert first["next_cursor"] is not None
    assert [item["id"] for item in second["items"]] == [imported[0]["id"]]
    assert second["next_cursor"] is None
    assert len({item["id"] for item in first["items"] + second["items"]}) == 3
    with pytest.raises(UploadError, match="^invalid_page_cursor$"):
        upload_service.cover_page(cursor="1:1", limit=2)


def test_cover_history_page_is_exposed_by_the_upload_api(settings):
    backend = RecordingBackend()
    app = create_app(settings)
    app.state.upload_service_factory = lambda root: UploadService(root, backend)
    payload = _png(400, 300)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/v1/uploads/session").json()["csrf_token"]
        client.headers["X-Upload-CSRF"] = token
        covers = []
        for index in range(2):
            response = client.post(
                f"/api/v1/uploads/covers?name=cover-{index}.png",
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 201
            covers.append(response.json())

        first = client.get("/api/v1/uploads/covers/page?limit=1")
        assert first.status_code == 200
        assert first.json()["items"] == [covers[1]]
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        second = client.get(
            "/api/v1/uploads/covers/page?limit=1&cursor=" + cursor
        )
        assert second.status_code == 200
        assert second.json() == {"items": [covers[0]], "next_cursor": None}
        assert client.get(
            "/api/v1/uploads/covers/page?limit=1&cursor=1:1"
        ).status_code == 409


def test_cover_preview_returns_the_bytes_from_its_verified_file_handle(
    upload_service, tmp_path, monkeypatch
):
    cover, payload = _cover(upload_service, tmp_path, "verified-preview.png", 400, 300)
    stored = upload_service.root / "assets" / f"{cover['id']}.png"
    original_read_bytes = Path.read_bytes

    def replace_on_second_path_read(path):
        if path == stored:
            replacement = b"unverified" * (3 * 1024 * 1024)
            path.write_bytes(replacement)
            return replacement
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", replace_on_second_path_read)
    content, mime_type = upload_service.cover_content(cover["id"])

    assert content == payload
    assert mime_type == "image/png"
    assert stored.stat().st_size == len(payload)


def test_cover_import_rejects_nonstandard_exif_orientation(upload_service, tmp_path):
    path = tmp_path / "oriented.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (400, 300), (32, 96, 192)).save(
        path, format="JPEG", exif=exif
    )

    with pytest.raises(UploadError, match="^invalid_cover_image$"):
        upload_service.import_cover(path, path.name)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        (
            "empty.png",
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", b"")
            + _png_chunk(b"IEND", b""),
        ),
        (
            "empty.jpg",
            b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x01\x01\xff\xda\x00\x02\xff\xd9",
        ),
        (
            "header-only.webp",
            b"RIFF\x16\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        ),
    ],
)
def test_cover_import_rejects_header_only_images(upload_service, tmp_path, name, payload):
    path = tmp_path / name
    path.write_bytes(payload)
    with pytest.raises(UploadError, match="^invalid_cover_image$"):
        upload_service.import_cover(path, name)


@pytest.mark.parametrize(
    ("name", "encoded", "mime_type"),
    [
        (
            "decoded.jpg",
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
            "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
            "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
            "MjIyMjL/wAARCAAwAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
            "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
            "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
            "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
            "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEB"
            "AQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJB"
            "UQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZH"
            "SElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaan"
            "qKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oA"
            "DAMBAAIRAxEAPwDx6iiiuo5gooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
            "igAooooAKKKKAP/2Q==",
            "image/jpeg",
        ),
        (
            "decoded.webp",
            "UklGRkQAAABXRUJQVlA4IDgAAABQAwCdASpAADAAPm02mEkkIqKhIqgAgA2JaQAAE/GT"
            "218PwAD++Op/8Gu9J8X+kulxpr0eOIAAAA==",
            "image/webp",
        ),
    ],
)
def test_cover_import_fully_decodes_valid_jpeg_and_webp(
    upload_service, tmp_path, name, encoded, mime_type
):
    path = tmp_path / name
    path.write_bytes(base64.b64decode(encoded))

    cover = upload_service.import_cover(path, name)

    assert cover["mime_type"] == mime_type
    assert (cover["width"], cover["height"]) == (64, 48)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        (
            "truncated-pixels.jpg",
            bytes.fromhex(
                "ffd8ffdb004300" + "01" * 64
                + "ffc400140001" + "00" * 15 + "00"
                + "ffc400141001" + "00" * 15 + "00"
                + "ffc0000b08012c019001011100"
                + "ffda0008010100003f0000ffd9"
            ),
        ),
        (
            "truncated-pixels.webp",
            bytes.fromhex(
                "524946461800000057454250565038200b0000003000009d012a90012c010000"
            ),
        ),
        (
            "truncated-lossless.webp",
            bytes.fromhex(
                "52494646160000005745425056384c0a0000002f8fc14a000000000000"
            ),
        ),
    ],
)
def test_cover_import_rejects_container_complete_but_undecodable_images(
    upload_service, tmp_path, name, payload
):
    path = tmp_path / name
    path.write_bytes(payload)
    with pytest.raises(UploadError, match="^invalid_cover_image$"):
        upload_service.import_cover(path, name)


def test_png_size_cap_never_calls_unbounded_decompressor_flush(
    upload_service, tmp_path, monkeypatch
):
    class CappedDecompressor:
        eof = True
        unused_data = b""
        unconsumed_tail = b"compressed-data-remains"

        def decompress(self, _payload, _maximum):
            return b""

        def flush(self):
            pytest.fail("unbounded zlib flush must not be called")

    monkeypatch.setattr(
        upload_service_module.zlib, "decompressobj", lambda: CappedDecompressor()
    )
    path = tmp_path / "bounded.png"
    path.write_bytes(_png(1, 1))
    with pytest.raises(UploadError, match="^invalid_cover_image$"):
        upload_service.import_cover(path, path.name)


@pytest.mark.parametrize(("expected_format", "mode"), [("JPEG", "RGB"), ("WEBP", "RGBA")])
def test_jpeg_and_webp_decoded_budget_is_checked_before_pixel_load(
    monkeypatch, expected_format, mode
):
    class OversizedDecodedImage:
        format = expected_format
        n_frames = 1
        size = (5000, 5000)

        def __init__(self):
            self.mode = mode

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getexif(self):
            return {}

        def load(self):
            raise AssertionError("pixel load must not run after the decoded budget fails")

    monkeypatch.setattr(
        upload_service_module.Image,
        "open",
        lambda *_args, **_kwargs: OversizedDecodedImage(),
    )

    assert upload_service_module._decoded_image_dimensions(
        b"synthetic image bytes", expected_format
    ) is None


def test_cover_in_use_cannot_be_deleted_until_draft_is_canceled(upload_service, tmp_path):
    account = _ready_account(upload_service, "bilibili")
    source = _source(upload_service, tmp_path)
    cover, _ = _cover(upload_service, tmp_path, "in-use.png", 400, 300)
    job = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="cover_in_use",
        target_overrides=[{
            "account_id": account["id"],
            "cover_landscape_asset_id": cover["id"],
        }],
    )[0]

    assert upload_service.cover(cover["id"])["active_reference_count"] == 1
    with pytest.raises(UploadError, match="^cover_in_use$"):
        upload_service.delete_cover_media(cover["id"])

    upload_service.cancel(job["id"])
    deleted = upload_service.delete_cover_media(cover["id"])
    assert deleted["media_state"] == "deleted"
    assert deleted["media_present"] is False


def test_bilibili_cover_schedule_and_options_reach_upload_request(upload_service, tmp_path):
    account = _ready_account(upload_service, "bilibili")
    source = _source(upload_service, tmp_path)
    cover, payload = _cover(upload_service, tmp_path, "bilibili.png", 400, 300)
    publish_at = _scheduled_at("bilibili")
    job = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="bilibili_full_parameters",
        target_overrides=[{
            "account_id": account["id"],
            "title": "B站参数标题",
            "tags": ["B站标签"],
            "cover_landscape_asset_id": cover["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
            "platform_options": {
                "dynamic": "发布动态文案",
                "no_reprint": True,
                "close_comments": True,
                "close_danmu": True,
            },
        }],
    )[0]

    upload_service.confirm(job["id"])
    _wait_until(lambda: len(upload_service.backend.uploads) == 1)
    request = upload_service.backend.uploads[0]
    assert request.platform == "bilibili"
    assert request.title == "B站参数标题"
    assert request.tags == ("B站标签",)
    assert upload_service.backend.cover_payloads[0][0] == payload
    assert not request.cover_landscape_path.exists()
    assert request.cover_portrait_path is None
    assert request.publish_at_unix == publish_at
    assert request.publish_timezone_offset_minutes == 570
    assert request.dynamic == "发布动态文案"
    assert request.no_reprint is True
    assert request.close_comments is True
    assert request.close_danmu is True


def test_verified_cover_bytes_are_staged_before_the_backend_reopens_them(tmp_path):
    class MutatingBackend(RecordingBackend):
        managed_path: Path

        def upload(self, request, stop):
            self.managed_path.write_bytes(b"x" * self.managed_path.stat().st_size)
            return super().upload(request, stop)

    backend = MutatingBackend()
    service = UploadService(tmp_path / "staged-cover", backend)
    service.start()
    try:
        account = _ready_account(service, "bilibili")
        source = _source(service, tmp_path)
        cover, payload = _cover(service, tmp_path, "staged-bilibili.png", 400, 300)
        backend.managed_path = service.root / "assets" / f"{cover['id']}.png"
        job = _create_jobs(
            service,
            source["id"],
            [account["id"]],
            key="staged_cover_bytes",
            target_overrides=[{
                "account_id": account["id"],
                "cover_landscape_asset_id": cover["id"],
            }],
        )[0]

        service.confirm(job["id"])
        _wait_until(lambda: len(backend.uploads) == 1)

        assert backend.cover_payloads[0][0] == payload
        assert backend.uploads[0].cover_landscape_path != backend.managed_path
        assert not backend.uploads[0].cover_landscape_path.exists()
    finally:
        service.stop()


def test_douyin_declaration_is_null_by_default_and_explicit_only_when_selected(
    upload_service, tmp_path
):
    accounts = [_ready_account(upload_service, "douyin") for _ in range(2)]
    source = _source(upload_service, tmp_path)
    jobs = _create_jobs(
        upload_service,
        source["id"],
        [account["id"] for account in accounts],
        key="douyin_declaration",
        target_overrides=[{
            "account_id": accounts[1]["id"],
            "platform_options": {"declaration": "内容为转载信息"},
        }],
    )

    assert jobs[0]["platform_options"] == {"declaration": None}
    assert jobs[1]["platform_options"] == {"declaration": "内容为转载信息"}
    for job in jobs:
        upload_service.confirm(job["id"])
    _wait_until(lambda: len(upload_service.backend.uploads) == 2)
    declarations = {
        request.account_id: request.declaration for request in upload_service.backend.uploads
    }
    assert declarations == {
        accounts[0]["id"]: None,
        accounts[1]["id"]: "内容为转载信息",
    }

    class DummyUploader:
        declaration = None

        async def apply_self_declaration(self, page):
            raise AssertionError("the pinned default declaration must not run")

    default_uploader = DummyUploader()
    install_statement_policy("douyin", default_uploader, {"declaration": None})
    assert default_uploader.declaration
    assert default_uploader.declaration != "内容由AI生成"
    assert asyncio.run(default_uploader.apply_self_declaration(None)) is None

    class SuccessfulUploader:
        declaration = "内容为转载信息"

        async def apply_self_declaration(self, page):
            # The pinned adapter returns None after the underlying strict
            # set_self_declaration call succeeds.
            return None

    class SelectedDeclaration:
        first = property(lambda self: self)

        def __init__(self, count):
            self._count = count

        def filter(self, **_kwargs):
            return self

        async def count(self):
            return self._count

    class DeclarationPage:
        def locator(self, selector):
            return SelectedDeclaration(0 if ":visible" in selector else 1)

        def get_by_text(self, *_args, **_kwargs):
            return SelectedDeclaration(0)

    explicit_uploader = SuccessfulUploader()
    install_statement_policy(
        "douyin", explicit_uploader, {"declaration": "内容为转载信息"}
    )
    assert asyncio.run(
        explicit_uploader.apply_self_declaration(DeclarationPage())
    ) is None


def test_douyin_cover_slots_enforce_the_imported_image_orientation(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "douyin")
    source = _source(upload_service, tmp_path)
    landscape, _ = _cover(upload_service, tmp_path, "douyin-wide.png", 400, 200)
    portrait, _ = _cover(upload_service, tmp_path, "douyin-tall.png", 200, 400)

    assert next(
        platform
        for platform in upload_service.status()["platforms"]
        if platform["id"] == "douyin"
    )["cover_slots"] == ["landscape", "portrait"]

    for key, override in (
        ("douyin_portrait_in_landscape", {"cover_landscape_asset_id": portrait["id"]}),
        ("douyin_landscape_in_portrait", {"cover_portrait_asset_id": landscape["id"]}),
    ):
        with pytest.raises(UploadError, match="^douyin_cover_orientation_invalid$"):
            _create_jobs(
                upload_service,
                source["id"],
                [account["id"]],
                key=key,
                target_overrides=[{"account_id": account["id"], **override}],
            )

    jobs = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="douyin_portrait_in_portrait",
        target_overrides=[{
            "account_id": account["id"],
            "cover_portrait_asset_id": portrait["id"],
        }],
    )
    assert jobs[0]["cover_landscape_asset_id"] is None
    assert jobs[0]["cover_portrait_asset_id"] == portrait["id"]


def test_persisted_douyin_cover_in_the_wrong_slot_fails_before_backend(
    tmp_path,
):
    backend = RecordingBackend()
    service = UploadService(tmp_path / "persisted-wrong-cover-slot", backend)
    account = _ready_account(service, "douyin")
    source = _source(service, tmp_path)
    portrait, _ = _cover(service, tmp_path, "persisted-tall.png", 200, 400)
    job = _create_jobs(
        service,
        source["id"],
        [account["id"]],
        key="persisted_wrong_cover_slot",
        target_overrides=[{
            "account_id": account["id"],
            "cover_portrait_asset_id": portrait["id"],
        }],
    )[0]
    with sqlite3.connect(service.database_path) as database:
        database.execute(
            "UPDATE jobs SET cover_landscape_asset_id=?,cover_portrait_asset_id=NULL "
            "WHERE id=?",
            (portrait["id"], job["id"]),
        )

    with pytest.raises(UploadError, match="^douyin_cover_orientation_invalid$"):
        service.confirm(job["id"])
    assert service.job(job["id"])["state"] == "draft"

    with sqlite3.connect(service.database_path) as database:
        database.execute("UPDATE jobs SET state='queued' WHERE id=?", (job["id"],))
    kind, claimed = service._claim()
    assert kind == "upload"
    service._execute_operation(kind, claimed)

    current = service.job(job["id"])
    assert (current["state"], current["code"]) == (
        "failed",
        "douyin_cover_orientation_invalid",
    )
    assert backend.uploads == []


def test_tencent_dual_cover_ratio_short_title_and_content_label(upload_service, tmp_path):
    account = _ready_account(upload_service, "tencent")
    source = _source(upload_service, tmp_path)
    landscape, landscape_payload = _cover(
        upload_service, tmp_path, "tencent-landscape.png", 400, 300
    )
    portrait, portrait_payload = _cover(
        upload_service, tmp_path, "tencent-portrait.png", 300, 400
    )
    square, _ = _cover(upload_service, tmp_path, "tencent-square.png", 300, 300)

    with pytest.raises(UploadError, match="^tencent_cover_ratio_invalid$"):
        _create_jobs(
            upload_service,
            source["id"],
            [account["id"]],
            key="tencent_bad_ratio",
            target_overrides=[{
                "account_id": account["id"],
                "cover_landscape_asset_id": square["id"],
            }],
        )

    publish_at = _scheduled_at("tencent")
    job = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="tencent_full_parameters",
        target_overrides=[{
            "account_id": account["id"],
            "cover_landscape_asset_id": landscape["id"],
            "cover_portrait_asset_id": portrait["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
            "platform_options": {
                "short_title": "视频号短标题测试",
                "content_label": "含AI生成内容",
            },
        }],
    )[0]

    upload_service.confirm(job["id"])
    _wait_until(lambda: len(upload_service.backend.uploads) == 1)
    request = upload_service.backend.uploads[0]
    assert upload_service.backend.cover_payloads[0] == (
        landscape_payload, portrait_payload,
    )
    assert not request.cover_landscape_path.exists()
    assert not request.cover_portrait_path.exists()
    assert request.short_title == "视频号短标题测试"
    assert request.content_label == "含AI生成内容"
    assert request.publish_at_unix == publish_at
    assert request.publish_timezone_offset_minutes == 570


def test_tencent_short_title_is_normalized_into_the_reviewable_snapshot(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "tencent")
    source = _source(upload_service, tmp_path)
    generated = _create_jobs(
        upload_service, source["id"], [account["id"]], key="generated_short_title",
        target_overrides=[{"account_id": account["id"], "title": "主文案!!!!!!!"}],
    )[0]
    explicit = _create_jobs(
        upload_service, source["id"], [account["id"]], key="normalized_short_title",
        target_overrides=[{
            "account_id": account["id"],
            "platform_options": {"short_title": "Hello world!!!!"},
        }],
    )[0]
    assert generated["platform_options"]["short_title"] == "主文案，精彩内"
    assert explicit["platform_options"]["short_title"] == "Helloworld"


def test_tencent_schedule_requires_whole_hour_and_at_most_28_days(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "tencent")
    source = _source(upload_service, tmp_path)
    valid = _scheduled_at("tencent")
    for key, publish_at, code in (
        ("minute", valid + 60, "tencent_schedule_requires_whole_hour"),
        ("far", valid + 29 * 24 * 3600, "tencent_schedule_too_far"),
    ):
        with pytest.raises(UploadError, match=f"^{code}$"):
            _create_jobs(
                upload_service, source["id"], [account["id"]], key="schedule_" + key,
                target_overrides=[{
                    "account_id": account["id"], "publish_at_unix": publish_at,
                    "publish_timezone_offset_minutes": 570,
                }],
            )


@pytest.mark.parametrize("platform", ["bilibili", "douyin", "tencent"])
def test_schedule_too_close_is_rejected_for_each_platform(upload_service, tmp_path, platform):
    account = _ready_account(upload_service, platform)
    source = _source(upload_service, tmp_path)
    publish_at = int(time.time()) + SCHEDULE_LEAD_SECONDS[platform]

    with pytest.raises(UploadError, match="^publish_time_too_soon$"):
        _create_jobs(
            upload_service,
            source["id"],
            [account["id"]],
            key=f"{platform}_schedule_too_close",
            target_overrides=[{
                "account_id": account["id"],
                "publish_at_unix": publish_at,
                "publish_timezone_offset_minutes": 570,
            }],
        )


def test_schedule_is_rechecked_at_confirm_without_calling_backend(tmp_path, monkeypatch):
    backend = RecordingBackend()
    service = UploadService(tmp_path / "confirm-expiry", backend)
    account = _ready_account(service, "bilibili")
    source = _source(service, tmp_path)
    publish_at = _scheduled_at("bilibili")
    job = _create_jobs(
        service,
        source["id"],
        [account["id"]],
        key="schedule_expired_at_confirm",
        target_overrides=[{
            "account_id": account["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
        }],
    )[0]

    monkeypatch.setattr(
        upload_service_module.time,
        "time",
        lambda: publish_at - SCHEDULE_LEAD_SECONDS["bilibili"],
    )
    with pytest.raises(UploadError, match="^publish_time_too_soon$"):
        service.confirm(job["id"])
    assert service.job(job["id"])["state"] == "draft"
    assert backend.uploads == []


def test_retry_copies_every_platform_parameter(upload_service, tmp_path):
    account = _ready_account(upload_service, "tencent")
    source = _source(upload_service, tmp_path)
    landscape, _ = _cover(upload_service, tmp_path, "retry-landscape.png", 400, 300)
    portrait, _ = _cover(upload_service, tmp_path, "retry-portrait.png", 300, 400)
    publish_at = _scheduled_at("tencent")
    original = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="retry_all_platform_parameters",
        target_overrides=[{
            "account_id": account["id"],
            "title": "重试保留的标题",
            "description": "重试保留的说明",
            "tags": ["重试", "参数"],
            "mode": "publish",
            "cover_landscape_asset_id": landscape["id"],
            "cover_portrait_asset_id": portrait["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
            "platform_options": {
                "short_title": "视频号短标题测试",
                "content_label": "含AI生成内容",
            },
        }],
    )[0]
    upload_service.cancel(original["id"])
    retry = upload_service.retry(original["id"])

    copied_fields = (
        "platform",
        "account_id",
        "source_id",
        "title",
        "description",
        "tags",
        "category_id",
        "mode",
        "copyright",
        "source_credit",
        "cover_landscape_asset_id",
        "cover_portrait_asset_id",
        "publish_at_unix",
        "publish_timezone_offset_minutes",
        "platform_options",
    )
    for field in copied_fields:
        assert retry[field] == original[field]
    assert retry["id"] != original["id"]
    assert retry["retry_of"] == original["id"]
    assert retry["state"] == "draft"


def test_generated_tencent_short_title_is_stable_through_retry_and_confirm(
    upload_service, tmp_path
):
    account = _ready_account(upload_service, "tencent")
    source = _source(upload_service, tmp_path)
    original = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="generated_short_title_lifecycle",
        target_overrides=[{"account_id": account["id"], "title": "batch"}],
    )[0]
    assert original["platform_options"]["short_title"] == "batch，精"

    upload_service.cancel(original["id"])
    retry = upload_service.retry(original["id"])
    assert retry["platform_options"]["short_title"] == "batch，精"

    upload_service.confirm(retry["id"])
    _wait_until(lambda: len(upload_service.backend.uploads) == 1)
    assert upload_service.backend.uploads[0].short_title == "batch，精"


def test_retry_rejects_a_new_expired_schedule_but_replays_existing_successor(
    upload_service, tmp_path, monkeypatch
):
    account = _ready_account(upload_service, "douyin")
    source = _source(upload_service, tmp_path)
    publish_at = _scheduled_at("douyin")

    expired = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="expired_schedule_retry",
        target_overrides=[{
            "account_id": account["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
        }],
    )[0]
    upload_service.cancel(expired["id"])
    monkeypatch.setattr(
        upload_service_module.time,
        "time",
        lambda: publish_at - SCHEDULE_LEAD_SECONDS["douyin"],
    )
    with pytest.raises(UploadError, match="^publish_time_too_soon$"):
        upload_service.retry(expired["id"])
    assert not any(job["retry_of"] == expired["id"] for job in upload_service.jobs())

    monkeypatch.undo()
    replayable = _create_jobs(
        upload_service,
        source["id"],
        [account["id"]],
        key="existing_schedule_retry",
        target_overrides=[{
            "account_id": account["id"],
            "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": 570,
        }],
    )[0]
    upload_service.cancel(replayable["id"])
    successor = upload_service.retry(replayable["id"])
    monkeypatch.setattr(
        upload_service_module.time,
        "time",
        lambda: publish_at - SCHEDULE_LEAD_SECONDS["douyin"],
    )
    assert upload_service.retry(replayable["id"])["id"] == successor["id"]
