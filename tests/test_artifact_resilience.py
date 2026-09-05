from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

import video_download_control.api as api_module
from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.adapters.base import DownloadResult, ProducedFile
from video_download_control.api import (
    _VerifiedAuxiliaryStreamingResponse,
    _VerifiedOriginalFileResponse,
    _VerifiedOriginalSnapshotResponse,
    create_app,
)
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
        await asyncio.Event().wait()
        raise AssertionError("connected request listener unexpectedly resumed")

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


def test_large_original_download_streams_verified_bytes_in_chunks(
    settings: Settings,
) -> None:
    payload = b"large-original\n" + bytes(range(256)) * 4097
    assert len(payload) > 1024 * 1024
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/original-resilience"]},
        )
        assert created.status_code == 201
        worker = Worker(
            worker_id="original-resilience-worker",
            repository=WorkerRepository(app.state.database),
            adapter=ScriptedFakeAdapter(payload=payload),
            asset_store=AssetStore(settings.data_root, min_free_bytes=0),
            verifier=NonEmptyTestVerifier(),
        )
        result = worker.run_once()
        assert result is not None and result.status == "ready"
        [asset] = client.get(
            f"/api/v1/batches/{created.json()['id']}/assets"
        ).json()

        messages = _asgi_get_messages(app, asset["download_url"])

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
    assert headers["accept-ranges"] == "bytes"
    assert len(nonempty_chunks) > 1
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


@pytest.mark.parametrize(
    "failure",
    [OSError("synthetic send failure"), asyncio.CancelledError()],
    ids=["send-failure", "request-canceled"],
)
def test_verified_original_response_closes_rolled_spool_on_send_failure(
    failure: BaseException,
) -> None:
    handle = tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b")
    payload = b"x" * 4096
    handle.write(payload)
    handle.seek(0)
    assert getattr(handle, "_rolled", False) is True
    response = _VerifiedOriginalFileResponse(
        handle,
        file_info=os.fstat(handle.fileno()),
        filename="asset-00000000-0000-0000-0000-000000000000.mp4",
        expected_sha256="0" * 64,
    )

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            raise failure

    async def request() -> None:
        with pytest.raises(type(failure)):
            await response(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/asset",
                    "raw_path": b"/asset",
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


def test_original_snapshot_response_outer_cancel_stops_copy_and_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytes(range(256)) * 4096
    source = tmp_path / "source.mp4"
    source.write_bytes(payload)
    first_read = threading.Event()
    source_closed = threading.Event()
    read_calls = 0
    real_open = Path.open

    class SlowReader:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._handle.close()
            source_closed.set()

        def fileno(self):
            return self._handle.fileno()

        def read(self, size=-1):
            nonlocal read_calls
            read_calls += 1
            chunk = self._handle.read(size)
            if chunk:
                first_read.set()
                time.sleep(0.01)
            return chunk

    def slow_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == source and args and args[0] == "rb":
            return SlowReader(handle)
        return handle

    spools = []
    real_spooled_file = api_module.tempfile.SpooledTemporaryFile

    def tracked_spool(**kwargs):
        spool = real_spooled_file(**kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(Path, "open", slow_open)
    monkeypatch.setattr(
        api_module.tempfile,
        "SpooledTemporaryFile",
        tracked_spool,
    )
    response = _VerifiedOriginalSnapshotResponse(
        source,
        file_info=source.stat(),
        filename="asset.mp4",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    messages = []

    async def request() -> None:
        request_delivered = False
        listener_stopped = asyncio.Event()
        receive_blocker = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            try:
                await receive_blocker.wait()
            except asyncio.CancelledError:
                listener_stopped.set()
                raise RuntimeError("synthetic listener cleanup failure") from None
            raise AssertionError("connected listener unexpectedly resumed")

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        task = asyncio.create_task(
            response(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/asset",
                    "raw_path": b"/asset",
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
        )
        await asyncio.to_thread(first_read.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert listener_stopped.is_set()

    asyncio.run(request())

    expected_reads = (
        len(payload) + api_module._VERIFIED_STREAM_CHUNK_BYTES - 1
    ) // api_module._VERIFIED_STREAM_CHUNK_BYTES
    assert read_calls < expected_reads
    assert source_closed.is_set()
    assert messages == []
    assert len(spools) == 1
    assert spools[0].closed is True


def test_original_snapshot_response_send_failure_stops_listener_and_closes_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * (1024 * 1024 + 1)
    source = tmp_path / "source.mp4"
    source.write_bytes(payload)
    spools = []
    real_spooled_file = api_module.tempfile.SpooledTemporaryFile

    def tracked_spool(**kwargs):
        spool = real_spooled_file(**kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(
        api_module.tempfile,
        "SpooledTemporaryFile",
        tracked_spool,
    )
    response = _VerifiedOriginalSnapshotResponse(
        source,
        file_info=source.stat(),
        filename="asset.mp4",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    async def request() -> None:
        request_delivered = False
        listener_stopped = asyncio.Event()
        receive_blocker = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            try:
                await receive_blocker.wait()
            finally:
                listener_stopped.set()

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                raise OSError("synthetic send failure")

        with pytest.raises(OSError, match="synthetic send failure"):
            await response(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/asset",
                    "raw_path": b"/asset",
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
        assert listener_stopped.is_set()

    asyncio.run(request())
    assert len(spools) == 1
    assert getattr(spools[0], "_rolled", False) is True
    assert spools[0].closed is True
