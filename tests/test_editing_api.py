from __future__ import annotations

import hashlib
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video_download_control.editing.api import install_editing_routes
from video_download_control.editing import api as editing_api
from video_download_control.editing.contracts import (
    EditRecipe,
    EditingError,
    RenderAsset,
    RenderResult,
    SegmentSpec,
)
from video_download_control.editing.service import EditingService, default_editing_root


class _Processor:
    def render(
        self,
        source,
        output_dir,
        recipe,
        *,
        cancel_event=None,
        expected_source_size=None,
        expected_source_sha256=None,
    ):
        assert source.read_bytes() == b"registered download video"
        assert expected_source_size == len(b"registered download video")
        assert expected_source_sha256 == hashlib.sha256(
            b"registered download video"
        ).hexdigest()
        assets = []
        for index, segment in enumerate(recipe.segments, start=1):
            path = output_dir / f"segment-{index:03d}.mp4"
            path.write_bytes(f"segment:{segment.start_ms}:{segment.end_ms}".encode())
            assets.append(
                RenderAsset(
                    kind="segment",
                    path=path,
                    name=path.name,
                    mime_type="video/mp4",
                    ordinal=index,
                    duration_ms=segment.end_ms - segment.start_ms,
                    width=1280,
                    height=720,
                    container="mov,mp4,m4a,3gp,3g2,mj2",
                    video_codec="h264",
                    audio_codec="aac",
                )
            )
        if recipe.cover:
            path = output_dir / "cover.png"
            path.write_bytes(b"synthetic png")
            assets.append(
                RenderAsset(
                    kind="cover",
                    path=path,
                    name=path.name,
                    mime_type="image/png",
                    ordinal=1,
                    width=1200,
                    height=900,
                    container="png",
                    video_codec="png",
                )
            )
        return RenderResult(status="ready", code="render_complete", assets=tuple(assets))


def _wait(client: TestClient, plan_id: str, state: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/edits/plans/{plan_id}")
        assert result.status_code == 200
        if result.json()["state"] == state:
            return result.json()
        time.sleep(0.02)
    raise AssertionError(f"plan {plan_id} did not reach {state}")


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Host": "127.0.0.1.evil.example"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:8888"},
        {"Origin": "http://user@127.0.0.1"},
        {"Origin": "http://127.0.0.1/path"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_editing_reads_reject_cross_origin_and_non_loopback(tmp_path: Path, headers):
    app = FastAPI()
    manager = install_editing_routes(app, data_root=tmp_path / "data")
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get("/api/v1/edits/session", headers=headers)
            assert response.status_code == 403
            assert response.json() == {"detail": "editing_request_forbidden"}
            assert manager._service is None
    finally:
        manager.stop()


def test_editing_rejects_duplicate_authority_headers(tmp_path: Path):
    app = FastAPI()
    manager = install_editing_routes(app, data_root=tmp_path / "data")
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(
                "/api/v1/edits/session",
                headers=[("Host", "127.0.0.1"), ("Host", "evil.example")],
            )
            assert response.status_code == 403
            response = client.get(
                "/api/v1/edits/session",
                headers=[
                    ("Origin", "http://127.0.0.1"),
                    ("Origin", "http://evil.example"),
                ],
            )
            assert response.status_code == 403
    finally:
        manager.stop()


def test_editing_manager_takes_root_exclusive_lease_before_service_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_service = editing_api.EditingService
    constructed: list[Path] = []

    def tracked_service(*args, **kwargs):
        constructed.append(Path(args[0]))
        return real_service(*args, **kwargs)

    monkeypatch.setattr(editing_api, "EditingService", tracked_service)
    first_app = FastAPI()
    second_app = FastAPI()
    first = install_editing_routes(first_app, data_root=tmp_path / "data")
    second = install_editing_routes(second_app, data_root=tmp_path / "data")
    try:
        assert first.get().status()["schema_version"] == 1
        assert len(constructed) == 1
        with pytest.raises(EditingError, match="editing_worker_busy"):
            second.get()
        assert len(constructed) == 1
        first.stop()
        with pytest.raises(EditingError, match="editing_manager_stopped"):
            first.get()
        assert second.get().status()["schema_version"] == 1
        assert len(constructed) == 2
    finally:
        first.stop()
        second.stop()


def test_worker_releases_root_lease_after_a_timed_out_stop(tmp_path: Path):
    started = Event()
    release = Event()
    payload = b"registered source"

    class BlockingProcessor:
        def render(
            self,
            source,
            output_dir,
            recipe,
            *,
            cancel_event=None,
            expected_source_size=None,
            expected_source_sha256=None,
        ):
            assert source.read_bytes() == payload
            assert expected_source_size == len(payload)
            assert expected_source_sha256 == hashlib.sha256(payload).hexdigest()
            started.set()
            assert release.wait(5)
            return RenderResult(status="canceled", code="canceled")

    root = default_editing_root(tmp_path / "data")
    first = editing_api.EditingManager(root, BlockingProcessor)
    second = editing_api.EditingManager(root, None)
    try:
        service = first.get()
        source_path = tmp_path / "source.mp4"
        source_path.write_bytes(payload)
        source = service.import_source(
            source_path,
            source_path.name,
            hashlib.sha256(payload).hexdigest(),
            idempotency_key="lease-source",
        )
        project = service.create_project(source["id"], "lease", "lease-project")
        draft = service.update_draft(
            project["id"],
            1,
            {
                "segments": [{"start_ms": 0, "end_ms": 1000, "label": "one"}],
                "cover": None,
                "translation": None,
                "dubbing": None,
            },
            "lease-draft",
        )
        plan = service.create_plan(project["id"], draft["version"], "lease-plan")
        service.confirm_plan(plan["id"])
        first.wake()
        assert started.wait(5)

        first.stop(timeout_seconds=0.01)
        with pytest.raises(EditingError, match="editing_worker_busy"):
            second.get()

        release.set()
        deadline = time.monotonic() + 5
        while True:
            try:
                assert second.get().status()["schema_version"] == 1
                break
            except EditingError as error:
                assert error.code == "editing_worker_busy"
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        release.set()
        first.stop(timeout_seconds=1)
        second.stop()


def test_stop_retains_root_lease_until_inflight_service_operation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    copied = Event()
    release = Event()
    finished = Event()
    payload = b"source copied before its database row"
    root = default_editing_root(tmp_path / "data")
    first = editing_api.EditingManager(root, None)
    second = editing_api.EditingManager(root, None)
    results: list[dict] = []
    errors: list[BaseException] = []
    try:
        service = first.get()
        source_path = tmp_path / "source.mp4"
        source_path.write_bytes(payload)
        original_copy = service._copy_and_hash

        def blocked_copy(source, destination, maximum):
            result = original_copy(source, destination, maximum)
            copied.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(service, "_copy_and_hash", blocked_copy)

        def import_source() -> None:
            try:
                results.append(
                    first.invoke(
                        "import_source",
                        source_path,
                        source_path.name,
                        hashlib.sha256(payload).hexdigest(),
                        idempotency_key="inflight-source",
                    )
                )
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        thread = Thread(target=import_source)
        thread.start()
        assert copied.wait(5)

        first.stop(timeout_seconds=0.01)
        with pytest.raises(EditingError, match="editing_worker_busy"):
            second.get()

        release.set()
        assert finished.wait(5)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []

        deadline = time.monotonic() + 5
        while True:
            try:
                recovered = second.get()
                break
            except EditingError as error:
                assert error.code == "editing_worker_busy"
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        assert recovered.source_path(results[0]["id"]).read_bytes() == payload
    finally:
        release.set()
        first.stop(timeout_seconds=1)
        second.stop()


def test_stop_between_claim_and_cancel_binding_sets_the_new_event(tmp_path: Path):
    claimed = Event()
    release_claim = Event()
    processor_finished = Event()
    observed_cancel: list[bool] = []
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output"
    output.mkdir()
    recipe = EditRecipe(segments=(SegmentSpec(0, 1000, "one"),))

    class Processor:
        def render(
            self,
            source,
            output_dir,
            recipe,
            *,
            cancel_event=None,
            expected_source_size=None,
            expected_source_sha256=None,
        ):
            observed_cancel.append(bool(cancel_event and cancel_event.is_set()))
            processor_finished.set()
            return RenderResult(status="canceled", code="canceled")

    class BarrierService:
        processor = Processor()

        def claim_next_plan(self):
            claimed.set()
            assert release_claim.wait(5)
            return {
                "id": "a" * 32,
                "claim_token": "b" * 32,
                "recipe": recipe.to_dict(),
            }

        def plan_cancellation_requested(self, _plan_id, _token):
            return False

        def source_identity_for_plan(self, _plan_id):
            return source, source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest()

        def output_dir_for_plan(self, _plan_id, _token):
            return output

        def complete_plan(self, _plan_id, _token, _result):
            return None

        def fail_plan(self, _plan_id, _token, _code, *, canceled=False):
            return None

    manager = editing_api.EditingManager(tmp_path / "edits", None)
    manager._service = BarrierService()  # type: ignore[assignment]
    thread = Thread(target=manager._worker)
    manager._thread = thread
    thread.start()
    try:
        assert claimed.wait(5)
        manager.stop(timeout_seconds=0.01)
        release_claim.set()
        assert processor_finished.wait(5)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert observed_cancel == [True]
    finally:
        release_claim.set()
        manager.stop(timeout_seconds=1)


def test_editing_page_is_lazy_and_mutations_are_same_origin_csrf_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_root = tmp_path / "data"
    source = tmp_path / "download.mp4"
    source.write_bytes(b"registered download video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = str(uuid4())
    app = FastAPI()
    manager = install_editing_routes(
        app,
        data_root=data_root,
        original_asset_resolver=lambda value: (source, digest)
        if value == asset_id
        else (_ for _ in ()).throw(KeyError(value)),
        processor_factory=_Processor,
    )
    edit_root = default_editing_root(data_root)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            page = client.get("/edits", params={"asset_id": asset_id})
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            assert 'aria-current="page">编辑</a>' in page.text
            assert not edit_root.exists()
            payload = {"name": "剪辑 A", "idempotency_key": "project-a"}
            assert client.post(f"/api/v1/edits/projects/assets/{asset_id}", json=payload).status_code == 403
            token = client.get("/api/v1/edits/session").json()["csrf_token"]
            rebound = client.post(
                f"/api/v1/edits/projects/assets/{asset_id}",
                json=payload,
                headers={
                    "Host": "evil.example",
                    "Origin": "http://evil.example",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Editing-CSRF": token,
                },
            )
            assert rebound.status_code == 403
            assert not edit_root.exists()
            headers = {
                "X-Editing-CSRF": token,
                "Origin": "http://127.0.0.1",
                "Sec-Fetch-Site": "same-origin",
            }
            client.headers.update(headers)
            created = client.post(
                f"/api/v1/edits/projects/assets/{asset_id}", json=payload
            )
            assert created.status_code == 201, created.text
            project = created.json()
            assert project["source_asset_id"] == asset_id
            assert project["source_sha256"] == digest
            assert source.read_bytes() == b"registered download video"
            copied = next((edit_root / "sources").glob("*.mp4"))
            assert copied.read_bytes() == source.read_bytes()
            assert copied.resolve() != source.resolve()

            draft = client.get(
                f"/api/v1/edits/projects/{project['id']}/draft"
            ).json()
            recipe = {
                "segments": [
                    {"start_ms": 0, "end_ms": 1000, "label": "开场"},
                    {"start_ms": 1200, "end_ms": 2500, "label": "正文"},
                ],
                "cover": {
                    "timestamp_ms": 500,
                    "aspect_ratio": "4:3",
                    "title": "封面标题",
                    "subtitle": "",
                },
                "translation": None,
                "dubbing": None,
            }
            saved = client.put(
                f"/api/v1/edits/projects/{project['id']}/draft",
                json={
                    "expected_version": draft["version"],
                    "recipe": recipe,
                    "idempotency_key": "draft-a",
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["version"] == 2
            stale = client.put(
                f"/api/v1/edits/projects/{project['id']}/draft",
                json={
                    "expected_version": 1,
                    "recipe": recipe,
                    "idempotency_key": "draft-stale",
                },
            )
            assert stale.status_code == 409
            assert stale.json() == {"detail": "draft_version_conflict"}

            planned = client.post(
                f"/api/v1/edits/projects/{project['id']}/plans",
                json={"expected_version": 2, "idempotency_key": "plan-a"},
            )
            assert planned.status_code == 201, planned.text
            plan = planned.json()
            assert plan["state"] == "review"
            assert client.get("/api/v1/edits/assets").json() == []
            confirmed = client.post(f"/api/v1/edits/plans/{plan['id']}/confirm")
            assert confirmed.status_code == 200
            finished = _wait(client, plan["id"], "ready")
            assert finished["code"] == "render_complete"
            outputs = client.get(
                "/api/v1/edits/assets", params={"project_id": project["id"]}
            ).json()
            assert [item["kind"] for item in outputs] == ["segment", "segment", "cover"]
            assert outputs[0]["duration_ms"] == 1000
            assert outputs[2]["width"] == 1200
            assert client.get(
                f"/api/v1/edits/assets/{outputs[0]['id']}/content"
            ).content == b"segment:0:1000"
            partial = client.get(
                f"/api/v1/edits/assets/{outputs[0]['id']}/content",
                headers={"Range": "bytes=2-8"},
            )
            assert partial.status_code == 206
            assert partial.headers["content-range"] == "bytes 2-8/14"
            assert partial.content == b"gment:0"
            assert manager.resolve_output(outputs[0]["id"])[1:] == (
                outputs[0]["sha256"],
                "segment-001.mp4",
            )
            assert source.read_bytes() == b"registered download video"

            service = manager.get()
            original_open_asset = service.open_asset

            def replace_after_verification(value: str):
                handle, info, chunk_digests, record = original_open_asset(value)
                path = service.asset_root / f"{value}{Path(record['name']).suffix}"
                replacement = b"x" * info.st_size
                path.write_bytes(replacement)
                return handle, info, chunk_digests, record

            monkeypatch.setattr(service, "open_asset", replace_after_verification)
            with pytest.raises(RuntimeError, match="verified original chunk changed"):
                client.get(f"/api/v1/edits/assets/{outputs[0]['id']}/content")
    finally:
        manager.stop()


def test_unconfigured_processor_cannot_queue_a_plan(tmp_path: Path):
    source = tmp_path / "download.mp4"
    source.write_bytes(b"video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = str(uuid4())
    app = FastAPI()
    manager = install_editing_routes(
        app,
        data_root=tmp_path / "data",
        original_asset_resolver=lambda _value: (source, digest),
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers["X-Editing-CSRF"] = client.get(
                "/api/v1/edits/session"
            ).json()["csrf_token"]
            project = client.post(
                f"/api/v1/edits/projects/assets/{asset_id}",
                json={"name": "No processor", "idempotency_key": "no-processor"},
            ).json()
            draft = client.put(
                f"/api/v1/edits/projects/{project['id']}/draft",
                json={
                    "expected_version": 1,
                    "idempotency_key": "no-processor-draft",
                    "recipe": {
                        "segments": [{"start_ms": 0, "end_ms": 1000, "label": "one"}],
                        "cover": None,
                        "translation": None,
                        "dubbing": None,
                    },
                },
            ).json()
            plan = client.post(
                f"/api/v1/edits/projects/{project['id']}/plans",
                json={"expected_version": draft["version"], "idempotency_key": "no-processor-plan"},
            ).json()
            rejected = client.post(f"/api/v1/edits/plans/{plan['id']}/confirm")
            assert rejected.status_code == 409
            assert rejected.json() == {"detail": "processor_not_configured"}
            assert client.get(f"/api/v1/edits/plans/{plan['id']}").json()["state"] == "review"
    finally:
        manager.stop()


def test_cancel_between_claim_and_event_binding_reaches_processor(
    tmp_path: Path, monkeypatch
):
    claimed = Event()
    release_claim = Event()
    processor_started = Event()
    processor_saw_cancel = Event()

    class BarrierService(EditingService):
        def claim_next_plan(self):
            result = super().claim_next_plan()
            if result is not None:
                claimed.set()
                assert release_claim.wait(5)
            return result

    class CancelAwareProcessor:
        def render(
            self,
            source,
            output_dir,
            recipe,
            *,
            cancel_event=None,
            expected_source_size=None,
            expected_source_sha256=None,
        ):
            processor_started.set()
            if cancel_event is not None and cancel_event.is_set():
                processor_saw_cancel.set()
                return RenderResult(status="canceled", code="canceled")
            return RenderResult(status="failed", code="cancel_was_lost")

    monkeypatch.setattr(editing_api, "EditingService", BarrierService)
    source = tmp_path / "download.mp4"
    source.write_bytes(b"video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset_id = str(uuid4())
    app = FastAPI()
    manager = install_editing_routes(
        app,
        data_root=tmp_path / "data",
        original_asset_resolver=lambda _value: (source, digest),
        processor_factory=CancelAwareProcessor,
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers["X-Editing-CSRF"] = client.get(
                "/api/v1/edits/session"
            ).json()["csrf_token"]
            project = client.post(
                f"/api/v1/edits/projects/assets/{asset_id}",
                json={"name": "Cancel race", "idempotency_key": "cancel-project"},
            ).json()
            draft = client.put(
                f"/api/v1/edits/projects/{project['id']}/draft",
                json={
                    "expected_version": 1,
                    "idempotency_key": "cancel-draft",
                    "recipe": {
                        "segments": [
                            {"start_ms": 0, "end_ms": 1000, "label": "one"}
                        ],
                        "cover": None,
                        "translation": None,
                        "dubbing": None,
                    },
                },
            ).json()
            plan = client.post(
                f"/api/v1/edits/projects/{project['id']}/plans",
                json={
                    "expected_version": draft["version"],
                    "idempotency_key": "cancel-plan",
                },
            ).json()
            assert client.post(
                f"/api/v1/edits/plans/{plan['id']}/confirm"
            ).status_code == 200
            assert claimed.wait(5)
            canceled = client.post(f"/api/v1/edits/plans/{plan['id']}/cancel")
            assert canceled.status_code == 200
            assert canceled.json()["state"] == "canceling"
            release_claim.set()
            assert processor_started.wait(5)
            assert processor_saw_cancel.wait(5)
            assert _wait(client, plan["id"], "canceled")["code"] == "canceled"
    finally:
        release_claim.set()
        manager.stop()
