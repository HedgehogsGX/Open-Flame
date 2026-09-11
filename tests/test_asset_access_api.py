from __future__ import annotations

import asyncio
import errno
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
from video_download_control.adapters import ScriptedFakeAdapter
from video_download_control.adapters.base import DownloadResult, ProducedFile
from video_download_control.api import create_app
from video_download_control.assets import AssetStore, NonEmptyTestVerifier
from video_download_control.config import Settings
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


class _AuxiliaryFakeAdapter(ScriptedFakeAdapter):
    def download(self, request, context, progress, cancellation):
        original = super().download(request, context, progress, cancellation)
        if not original.files:
            return original
        media_key = original.files[0].media_key
        thumbnail = request.output_dir / "source.thumbnail.webp"
        caption = request.output_dir / "source.caption.en-US.vtt"
        thumbnail.write_bytes(b"safe thumbnail bytes\n")
        caption.write_bytes(b"WEBVTT\n\n00:00.000 --> 00:01.000\nSafe caption\n")
        return DownloadResult(
            files=original.files,
            thumbnails=(
                ProducedFile(
                    path=thumbnail,
                    media_key=media_key,
                    media_kind="image",
                    role="thumbnail",
                    ordinal=0,
                ),
            ),
            captions=(
                ProducedFile(
                    path=caption,
                    media_key=media_key,
                    media_kind="text",
                    role="caption",
                    ordinal=0,
                ),
            ),
        )


def _finish_fake_download(
    app,
    settings: Settings,
    *,
    payload: bytes,
    adapter: ScriptedFakeAdapter | None = None,
) -> None:
    worker = Worker(
        worker_id="asset-api-test-worker",
        repository=WorkerRepository(app.state.database),
        adapter=adapter or ScriptedFakeAdapter(payload=payload),
        asset_store=AssetStore(settings.data_root, min_free_bytes=0),
        verifier=NonEmptyTestVerifier(),
    )
    result = worker.run_once()
    assert result is not None
    assert result.status == "ready"


def _create_ready_auxiliary_asset(
    client: TestClient,
    app,
    settings: Settings,
    *,
    suffix: str,
) -> dict:
    created = client.post(
        "/api/v1/batches",
        json={"inputs": [f"https://youtu.be/{suffix}"]},
    ).json()
    _finish_fake_download(
        app,
        settings,
        payload=b"original bytes\n",
        adapter=_AuxiliaryFakeAdapter(payload=b"original bytes\n"),
    )
    response = client.get(f"/api/v1/batches/{created['id']}/assets")
    assert response.status_code == 200
    return response.json()[0]


def test_ready_asset_lists_and_downloads_registered_auxiliary_artifacts(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={
                "name": "private-title-must-not-leak",
                "inputs": ["https://youtu.be/asset-api-auxiliary"],
            },
        ).json()
        _finish_fake_download(
            app,
            settings,
            payload=b"original bytes\n",
            adapter=_AuxiliaryFakeAdapter(payload=b"original bytes\n"),
        )

        listed = client.get(f"/api/v1/batches/{created['id']}/assets")
        asset = listed.json()[0]
        downloads = [
            client.get(artifact["download_url"])
            for artifact in asset["artifacts"]
        ]
        log_status = client.get("/api/v1/operations/logs?limit=100").json()

    assert listed.status_code == 200
    assert [artifact["kind"] for artifact in asset["artifacts"]] == [
        "thumbnail",
        "caption",
    ]
    assert [artifact["mime_type"] for artifact in asset["artifacts"]] == [
        "image/webp",
        "text/vtt",
    ]
    assert [artifact["language"] for artifact in asset["artifacts"]] == [
        None,
        "en-US",
    ]
    assert all(len(artifact["sha256"]) == 64 for artifact in asset["artifacts"])
    assert all(
        artifact["download_url"]
        == f"/api/v1/artifacts/{artifact['artifact_id']}/download"
        for artifact in asset["artifacts"]
    )
    assert "path" not in listed.text
    assert "private-title-must-not-leak" not in listed.text

    assert [response.content for response in downloads] == [
        b"safe thumbnail bytes\n",
        b"WEBVTT\n\n00:00.000 --> 00:01.000\nSafe caption\n",
    ]
    assert [response.headers["content-type"] for response in downloads] == [
        "image/webp",
        "text/vtt; charset=utf-8",
    ]
    for artifact, response in zip(asset["artifacts"], downloads, strict=True):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith(
            f'attachment; filename="artifact-{artifact["artifact_id"]}'
        )
        assert "private-title-must-not-leak" not in disposition

    assert log_status["rejected_events"] == 0
    artifact_download_events = [
        event
        for event in log_status["events"]
        if event["event"] == "http.request_completed"
        and event.get("route") == "/api/v1/artifacts/{artifact_id}/download"
    ]
    assert len(artifact_download_events) == 2
    assert all(event["status_code"] == 200 for event in artifact_download_events)


def test_auxiliary_download_requires_canonical_registered_ready_sidecar(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-aux-authz"]},
        ).json()
        _finish_fake_download(
            app,
            settings,
            payload=b"original bytes\n",
            adapter=_AuxiliaryFakeAdapter(payload=b"original bytes\n"),
        )
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        thumbnail = asset["artifacts"][0]
        with app.state.database.connect() as connection:
            manifest_id = connection.execute(
                "SELECT id FROM artifacts WHERE asset_id = ? AND kind = 'manifest'",
                (asset["asset_id"],),
            ).fetchone()[0]

        invalid = client.get("/api/v1/artifacts/not-a-uuid/download")
        unregistered = client.get(
            "/api/v1/artifacts/00000000-0000-0000-0000-000000000000/download"
        )
        noncanonical = client.get(
            f"/api/v1/artifacts/{thumbnail['artifact_id'].upper()}/download"
        )
        manifest = client.get(f"/api/v1/artifacts/{manifest_id}/download")

        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET status = 'failed' WHERE id = ?",
                (asset["job_id"],),
            )
        nonready_job = client.get(thumbnail["download_url"])
        with app.state.database.connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET status = 'ready' WHERE id = ?",
                (asset["job_id"],),
            )
            connection.execute(
                "UPDATE media_assets SET status = 'failed' WHERE id = ?",
                (asset["asset_id"],),
            )
        nonready_asset = client.get(thumbnail["download_url"])

    for response in (
        invalid,
        unregistered,
        noncanonical,
        manifest,
        nonready_job,
        nonready_asset,
    ):
        assert response.status_code == 404
        assert response.json() == {"detail": "辅助产物不存在"}


@pytest.mark.parametrize("integrity_break", ["duplicate_original", "hash_mismatch"])
def test_auxiliary_download_requires_one_matching_parent_original(
    settings: Settings,
    integrity_break: str,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        asset = _create_ready_auxiliary_asset(
            client,
            app,
            settings,
            suffix=f"asset-api-parent-{integrity_break}",
        )
        thumbnail = asset["artifacts"][0]
        with app.state.database.connect() as connection:
            original = connection.execute(
                """
                SELECT path, sha256, created_at
                FROM artifacts
                WHERE asset_id = ? AND kind = 'original'
                """,
                (asset["asset_id"],),
            ).fetchone()
            if integrity_break == "duplicate_original":
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        id, asset_id, kind, path, mime_type, sha256,
                        parent_artifact_id, tool_name, tool_version, created_at
                    ) VALUES (?, ?, 'original', ?, NULL, ?, NULL, NULL, NULL, ?)
                    """,
                    (
                        str(uuid4()),
                        asset["asset_id"],
                        original["path"],
                        original["sha256"],
                        original["created_at"],
                    ),
                )
            else:
                connection.execute(
                    "UPDATE artifacts SET sha256 = ? WHERE asset_id = ? AND kind = 'original'",
                    ("0" * 64, asset["asset_id"]),
                )

        rejected = client.get(thumbnail["download_url"])

    assert rejected.status_code == 404
    assert rejected.json() == {"detail": "辅助产物不存在"}


@pytest.mark.parametrize(
    "corruption",
    [
        "outside_path",
        "wrong_directory",
        "wrong_mime",
        "wrong_hash",
        "modified_content",
        "empty_content",
    ],
)
def test_auxiliary_download_rejects_invalid_registered_file(
    settings: Settings,
    corruption: str,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        asset = _create_ready_auxiliary_asset(
            client,
            app,
            settings,
            suffix=f"asset-api-corrupt-{corruption}",
        )
        thumbnail = asset["artifacts"][0]
        with app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT path FROM artifacts WHERE id = ?",
                (thumbnail["artifact_id"],),
            ).fetchone()
            artifact_path = settings.data_root / row["path"]
            if corruption == "outside_path":
                outside = settings.data_root.parent / "outside-thumbnail.webp"
                outside.write_bytes(artifact_path.read_bytes())
                connection.execute(
                    "UPDATE artifacts SET path = '../outside-thumbnail.webp' WHERE id = ?",
                    (thumbnail["artifact_id"],),
                )
            elif corruption == "wrong_directory":
                wrong = (
                    settings.data_root
                    / "assets"
                    / asset["asset_id"]
                    / "captions"
                    / "thumbnail-0000.webp"
                )
                wrong.write_bytes(artifact_path.read_bytes())
                connection.execute(
                    "UPDATE artifacts SET path = ? WHERE id = ?",
                    (
                        wrong.relative_to(settings.data_root).as_posix(),
                        thumbnail["artifact_id"],
                    ),
                )
            elif corruption == "wrong_mime":
                connection.execute(
                    "UPDATE artifacts SET mime_type = 'image/png' WHERE id = ?",
                    (thumbnail["artifact_id"],),
                )
            elif corruption == "wrong_hash":
                connection.execute(
                    "UPDATE artifacts SET sha256 = ? WHERE id = ?",
                    ("0" * 64, thumbnail["artifact_id"]),
                )
            elif corruption == "modified_content":
                artifact_path.write_bytes(b"modified after registration\n")
            else:
                artifact_path.write_bytes(b"")

        rejected = client.get(thumbnail["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "辅助产物文件不可用"}
    assert "outside-thumbnail" not in rejected.text


def test_auxiliary_download_rejects_hard_linked_file(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        asset = _create_ready_auxiliary_asset(
            client,
            app,
            settings,
            suffix="asset-api-aux-hardlink",
        )
        thumbnail = asset["artifacts"][0]
        with app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT path FROM artifacts WHERE id = ?",
                (thumbnail["artifact_id"],),
            ).fetchone()
        artifact_path = settings.data_root / row["path"]
        alias = artifact_path.with_name("thumbnail-hard-link.webp")
        try:
            os.link(artifact_path, alias)
        except OSError as exc:
            pytest.skip(f"hard links are unavailable on this filesystem: {exc}")

        rejected = client.get(thumbnail["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "辅助产物文件不可用"}


def test_ready_batch_lists_original_metadata_and_downloads_registered_file(
    settings: Settings,
) -> None:
    media = b"registered original bytes\n"
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={
                "name": "title-must-not-enter-download-header",
                "inputs": ["https://youtu.be/asset-api-ready"],
            },
        ).json()
        batch_id = created["id"]
        job_id = created["jobs"][0]["id"]

        pending = client.get(f"/api/v1/batches/{batch_id}/assets")
        _finish_fake_download(app, settings, payload=media)
        listed = client.get(f"/api/v1/batches/{batch_id}/assets")

        assert listed.status_code == 200
        assets = listed.json()
        assert len(assets) == 1
        asset = assets[0]
        downloaded = client.get(asset["download_url"])
        log_status = client.get("/api/v1/operations/logs?limit=100").json()

    assert pending.status_code == 200
    assert pending.json() == []
    assert set(asset) == {
        "asset_id",
        "job_id",
        "ordinal",
        "original",
        "download_url",
        "artifacts",
    }
    assert asset["artifacts"] == []
    assert asset["job_id"] == job_id
    assert asset["ordinal"] == 0
    assert asset["download_url"] == (
        f"/api/v1/assets/{asset['asset_id']}/download"
    )
    assert asset["original"] == {
        "media_kind": "video",
        "duration_seconds": None,
        "container": "fake",
        "codec": None,
        "width": None,
        "height": None,
        "size_bytes": len(media),
        "sha256": asset["original"]["sha256"],
    }
    assert len(asset["original"]["sha256"]) == 64
    assert "path" not in listed.text
    assert "title-must-not-enter-download-header" not in listed.text

    assert downloaded.status_code == 200
    assert downloaded.content == media
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    disposition = downloaded.headers["content-disposition"]
    assert disposition == f'attachment; filename="asset-{asset["asset_id"]}.fake"'
    assert "title-must-not-enter-download-header" not in disposition
    assert log_status["rejected_events"] == 0


def test_asset_download_rejects_unregistered_missing_and_outside_paths(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        missing_batch = client.get("/api/v1/batches/not-found/assets")
        missing_asset = client.get(
            "/api/v1/assets/00000000-0000-0000-0000-000000000000/download"
        )
        invalid_asset = client.get("/api/v1/assets/not-a-uuid/download")

        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-containment"]},
        ).json()
        _finish_fake_download(app, settings, payload=b"inside only\n")
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        outside = settings.data_root.parent / "outside.fake"
        outside.write_bytes(b"must never be served")
        with app.state.database.connect() as connection:
            connection.execute(
                """
                UPDATE artifacts SET path = '../outside.fake'
                WHERE asset_id = ? AND kind = 'original'
                """,
                (asset["asset_id"],),
            )
        escaped = client.get(asset["download_url"])

    assert missing_batch.status_code == 404
    assert missing_asset.status_code == 404
    assert invalid_asset.status_code == 404
    assert escaped.status_code == 409
    assert escaped.json() == {"detail": "成品文件不可用"}
    assert outside.read_bytes() == b"must never be served"


def test_asset_download_rejects_same_size_content_drift(settings: Settings) -> None:
    payload = b"registered immutable bytes\n"
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-same-size-drift"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        registered = app.state.batch_service.get_ready_original_asset(
            asset["asset_id"]
        )
        assert registered is not None
        original = settings.data_root.joinpath(
            *Path(registered["original_path"]).parts
        )
        original.write_bytes(b"x" * len(payload))

        rejected = client.get(asset["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "成品文件不可用"}


def test_asset_download_closes_spool_when_temporary_storage_is_full(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-full-spool"]},
        ).json()
        _finish_fake_download(app, settings, payload=b"registered bytes\n")
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]

        class FullSpool:
            closed = False

            def write(self, _chunk: bytes) -> None:
                raise OSError(errno.ENOSPC, "synthetic temporary storage full")

            def close(self) -> None:
                self.closed = True

        spool = FullSpool()
        monkeypatch.setattr(
            api_module.tempfile,
            "SpooledTemporaryFile",
            lambda **_kwargs: spool,
        )
        rejected = client.get(asset["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "成品文件不可用"}
    assert spool.closed is True


def test_asset_snapshot_stops_cooperatively_when_client_disconnects(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytes(range(256)) * 4096
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-snapshot-cancel"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        registered = app.state.batch_service.get_ready_original_asset(
            asset["asset_id"]
        )
        assert registered is not None
        original = settings.data_root.joinpath(
            *Path(registered["original_path"]).parts
        )

        first_read = threading.Event()
        read_calls = 0
        real_open = Path.open

        class SlowReader:
            def __init__(self, handle):
                self._handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._handle.close()

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
            if path == original and args and args[0] == "rb":
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
        messages = []
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.to_thread(first_read.wait)
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        async def request():
            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": asset["download_url"],
                    "raw_path": asset["download_url"].encode("ascii"),
                    "query_string": b"",
                    "root_path": "",
                    "headers": [(b"host", b"127.0.0.1")],
                    "client": ("testclient", 50000),
                    "server": ("testserver", 80),
                    "state": {},
                },
                receive,
                send,
            )

        asyncio.run(request())

    expected_reads = (
        len(payload) + api_module._VERIFIED_STREAM_CHUNK_BYTES - 1
    ) // api_module._VERIFIED_STREAM_CHUNK_BYTES
    assert read_calls < expected_reads
    [start] = [
        message for message in messages if message["type"] == "http.response.start"
    ]
    assert start["status"] == api_module._CLIENT_CLOSED_REQUEST_STATUS
    assert all(
        not message.get("body")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert len(spools) == 1
    assert spools[0].closed is True


def test_verified_asset_download_preserves_byte_ranges(settings: Settings) -> None:
    payload = b"0123456789-range-payload"
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-range"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]

        partial = client.get(
            asset["download_url"],
            headers={"Range": "bytes=3-9"},
        )
        unsatisfiable = client.get(
            asset["download_url"],
            headers={"Range": "bytes=999-"},
        )
        multipart = client.get(
            asset["download_url"],
            headers={"Range": "bytes=0-1,4-5"},
        )

    assert partial.status_code == 206
    assert partial.content == payload[3:10]
    assert partial.headers["content-range"] == f"bytes 3-9/{len(payload)}"
    assert partial.headers["content-length"] == "7"
    assert partial.headers["accept-ranges"] == "bytes"
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{len(payload)}"
    assert multipart.status_code == 206
    assert multipart.headers["content-type"].startswith("multipart/byteranges;")
    assert int(multipart.headers["content-length"]) == len(multipart.content)
    assert b"01" in multipart.content
    assert b"45" in multipart.content


def test_asset_download_serves_verified_snapshot_if_path_changes_after_hash(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"registered snapshot bytes\n"
    replacement = b"x" * len(payload)
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-after-hash-swap"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        registered = app.state.batch_service.get_ready_original_asset(
            asset["asset_id"]
        )
        assert registered is not None
        original = settings.data_root.joinpath(
            *Path(registered["original_path"]).parts
        )
        real_sha256 = api_module.hashlib.sha256
        swapped = False

        class SwapAfterHash:
            def __init__(self):
                self._delegate = real_sha256()

            def update(self, chunk: bytes) -> None:
                self._delegate.update(chunk)

            def hexdigest(self) -> str:
                nonlocal swapped
                if not swapped:
                    original.write_bytes(replacement)
                    swapped = True
                return self._delegate.hexdigest()

        monkeypatch.setattr(api_module.hashlib, "sha256", SwapAfterHash)
        downloaded = client.get(asset["download_url"])

    assert swapped is True
    assert original.read_bytes() == replacement
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["etag"] == f'"{asset["original"]["sha256"]}"'


def test_asset_download_rejects_content_changed_while_hashing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"a" * (192 * 1024)
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-during-hash-swap"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        registered = app.state.batch_service.get_ready_original_asset(
            asset["asset_id"]
        )
        assert registered is not None
        original = settings.data_root.joinpath(
            *Path(registered["original_path"]).parts
        )
        real_open = Path.open
        mutated = False

        class MutatingReader:
            def __init__(self, handle):
                self._handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._handle.close()

            def fileno(self):
                return self._handle.fileno()

            def read(self, size=-1):
                nonlocal mutated
                chunk = self._handle.read(size)
                if chunk and not mutated:
                    with real_open(original, "r+b") as writer:
                        writer.seek(len(payload) - 1)
                        writer.write(b"x")
                        writer.flush()
                        os.fsync(writer.fileno())
                    mutated = True
                return chunk

        def mutating_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path == original and args and args[0] == "rb":
                return MutatingReader(handle)
            return handle

        monkeypatch.setattr(Path, "open", mutating_open)
        rejected = client.get(asset["download_url"])

    assert mutated is True
    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "成品文件不可用"}


def test_asset_download_supports_independent_concurrent_readers(
    settings: Settings,
) -> None:
    payload = bytes(range(256)) * 4096
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-concurrent-readers"]},
        ).json()
        _finish_fake_download(app, settings, payload=payload)
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: client.get(asset["download_url"]),
                    range(2),
                )
            )

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.content for response in responses] == [payload, payload]


def test_asset_download_rejects_a_hard_linked_original(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        created = client.post(
            "/api/v1/batches",
            json={"inputs": ["https://youtu.be/asset-api-linked"]},
        ).json()
        _finish_fake_download(app, settings, payload=b"single link required\n")
        asset = client.get(
            f"/api/v1/batches/{created['id']}/assets"
        ).json()[0]
        with app.state.database.connect() as connection:
            row = connection.execute(
                """
                SELECT path FROM artifacts
                WHERE asset_id = ? AND kind = 'original'
                """,
                (asset["asset_id"],),
            ).fetchone()
        original = settings.data_root / row["path"]
        alias = original.with_name("hard-link-alias.fake")
        os.link(original, alias)

        rejected = client.get(asset["download_url"])

    assert rejected.status_code == 409
    assert rejected.json() == {"detail": "成品文件不可用"}


def test_frontend_fetches_ready_assets_and_builds_links_without_inner_html(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        client.headers["X-Download-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        page = client.get("/")

    assert page.status_code == 200
    assert 'id="asset-links"' in page.text
    assert 'id="asset-list"' in page.text
    assert 'id="job-progress"' in page.text
    assert 'id="job-progress-list"' in page.text
    assert "任务进度（阶段估算）" in page.text
    assert 'id="recent-batches"' in page.text
    assert "async function openBatch(batchId)" in page.text
    assert "async function loadRecentBatches()" in page.text
    assert "openBatch(item._batchId)" in page.text
    assert "reconcileKeyed(" in page.text
    assert "async function loadReadyAssets(payload, generation)" in page.text
    assert "/api/v1/batches/${encodeURIComponent(payload.id)}/assets" in page.text
    assert "item._downloadLink = document.createElement('a');" in page.text
    assert "item._downloadLink.href = asset.download_url;" in page.text
    assert "item._downloadLink.textContent = `下载成品" in page.text
    assert "artifacts.map((artifact, artifactIndex)" in page.text
    assert "artifactItem._link.href = artifact.download_url;" in page.text
    assert "artifact.kind === 'thumbnail' ? '缩略图' : '字幕'" in page.text
    assert "function renderJobProgress(payload)" in page.text
    assert "postprocessing: '正在合并/后处理'" in page.text
    assert "item._progress.value = percent;" in page.text
    assert "renderJobProgress(payload);" in page.text
    assert "innerHTML" not in page.text
