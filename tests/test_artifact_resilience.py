from __future__ import annotations

import asyncio
import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.adapters.base import DownloadResult, ProducedFile
from video_download_control.api import _VerifiedAuxiliaryStreamingResponse, create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.config import Settings
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


_ORIGINAL_PAYLOAD = b"resilient original bytes\n"


class _AuxiliaryPayloadAdapter(ScriptedFakeAdapter):
    def __init__(
        self,
        *,
        thumbnail_payload: bytes,
        caption_payload: bytes | None = None,
    ) -> None:
        super().__init__(payload=_ORIGINAL_PAYLOAD)
        self.thumbnail_payload = thumbnail_payload
        self.caption_payload = caption_payload

    def download(self, request, context, progress, cancellation):
        original = super().download(
            request,
            context,
            progress,
            cancellation,
        )
        if not original.files:
            return original
        media_key = original.files[0].media_key
        thumbnail = request.output_dir / "source.thumbnail.webp"
        thumbnail.write_bytes(self.thumbnail_payload)
        thumbnails = (
            ProducedFile(
                path=thumbnail,
                media_key=media_key,
                media_kind="image",
                role="thumbnail",
                ordinal=0,
            ),
        )
        captions: tuple[ProducedFile, ...] = ()
        if self.caption_payload is not None:
            caption = request.output_dir / "source.caption.en-US.vtt"
            caption.write_bytes(self.caption_payload)
            captions = (
                ProducedFile(
                    path=caption,
                    media_key=media_key,
                    media_kind="text",
                    role="caption",
                    ordinal=0,
                ),
            )
        return DownloadResult(
            files=original.files,
            thumbnails=thumbnails,
            captions=captions,
        )


def _create_ready_auxiliary_asset(
    client: TestClient,
    app,
    settings: Settings,
    *,
    thumbnail_payload: bytes,
    caption_payload: bytes | None = None,
) -> tuple[str, dict[str, Any]]:
    created = client.post(
        "/api/v1/batches",
        json={"inputs": ["https://youtu.be/artifact-resilience"]},
    )
    assert created.status_code == 201

    worker = Worker(
        worker_id="artifact-resilience-worker",
        repository=WorkerRepository(app.state.database),
        adapter=_AuxiliaryPayloadAdapter(
            thumbnail_payload=thumbnail_payload,
            caption_payload=caption_payload,
        ),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )
    result = worker.run_once()
    assert result is not None
    assert result.status == "ready"

    batch_id = created.json()["id"]
    listed = client.get(f"/api/v1/batches/{batch_id}/assets")
    assert listed.status_code == 200
    [asset] = listed.json()
    return batch_id, asset


def _asgi_get_messages(app, path: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    request_delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def request() -> None:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"host", b"testserver")],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )

    asyncio.run(request())
    return messages


def test_asset_listing_skips_malformed_auxiliary_rows_and_keeps_original(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        batch_id, valid_asset = _create_ready_auxiliary_asset(
            client,
            app,
            settings,
            thumbnail_payload=b"valid thumbnail\n",
            caption_payload=b"WEBVTT\n\n00:00.000 --> 00:01.000\nValid caption\n",
        )
        by_kind = {
            artifact["kind"]: artifact for artifact in valid_asset["artifacts"]
        }
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE artifacts SET mime_type = '' WHERE id = ?",
                (by_kind["thumbnail"]["artifact_id"],),
            )
            connection.execute(
                "UPDATE artifacts SET sha256 = 'not-a-sha256' WHERE id = ?",
                (by_kind["caption"]["artifact_id"],),
            )

        listed = client.get(f"/api/v1/batches/{batch_id}/assets")

    assert listed.status_code == 200
    [asset] = listed.json()
    assert asset["asset_id"] == valid_asset["asset_id"]
    assert asset["original"] == valid_asset["original"]
    assert asset["download_url"] == valid_asset["download_url"]
    assert asset["artifacts"] == []


def test_large_auxiliary_download_streams_with_exact_content_length(
    settings: Settings,
) -> None:
    payload = b"large-sidecar\n" + bytes(range(256)) * 4097
    assert len(payload) > 1024 * 1024
    app = create_app(settings)
    with TestClient(app) as client:
        _, asset = _create_ready_auxiliary_asset(
            client,
            app,
            settings,
            thumbnail_payload=payload,
        )
        [artifact] = asset["artifacts"]

        messages = _asgi_get_messages(app, artifact["download_url"])

    [start] = [
        message for message in messages if message["type"] == "http.response.start"
    ]
    headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    body_messages = [
        message for message in messages if message["type"] == "http.response.body"
    ]
    nonempty_chunks = [
        message["body"] for message in body_messages if message["body"]
    ]

    assert start["status"] == 200
    assert headers["content-length"] == str(len(payload))
    assert len(nonempty_chunks) > 1
    assert any(message.get("more_body") is True for message in body_messages)
    assert body_messages[-1].get("more_body", False) is False
    assert b"".join(message["body"] for message in body_messages) == payload


def test_verified_auxiliary_response_closes_rolled_spool_on_send_failure() -> None:
    handle = tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b")
    payload = b"x" * 4096
    handle.write(payload)
    handle.seek(0)
    assert getattr(handle, "_rolled", False) is True
    response = _VerifiedAuxiliaryStreamingResponse(
        handle,
        size_bytes=len(payload),
        media_type="image/webp",
        filename="artifact-00000000-0000-0000-0000-000000000000.webp",
    )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("synthetic send failure")

    async def request() -> None:
        with pytest.raises(ClientDisconnect):
            await response(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/artifact",
                    "raw_path": b"/artifact",
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"host", b"testserver")],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                    "state": {},
                },
                receive,
                send,
            )

    asyncio.run(request())
    assert handle.closed is True
