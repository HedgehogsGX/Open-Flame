from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from video_download_control.editing import (
    EditingError,
    EditingService,
    RenderAsset,
    RenderResult,
    default_editing_root,
)


SOURCE_ASSET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def recipe(**updates):
    value = {
        "segments": [
            {"start_ms": 0, "end_ms": 1200, "label": "开场"},
            {"start_ms": 1500, "end_ms": 3000, "label": "重点"},
        ],
        "cover": {
            "timestamp_ms": 500, "aspect_ratio": "16:9",
            "title": "封面标题", "subtitle": "副标题",
        },
        "translation": {
            "enabled": False, "source_language": "auto", "target_language": "",
            "provider": "", "model": "", "state": "disabled",
        },
        "dubbing": {
            "enabled": False, "language": "", "provider": "", "model": "",
            "voice": "", "state": "disabled", "replace_original_audio": False,
        },
    }
    value.update(updates)
    return value


@pytest.fixture
def editing(tmp_path):
    return EditingService(tmp_path / "data-edits")


def imported(editing, tmp_path):
    path = tmp_path / "original.mp4"
    payload = b"immutable download original"
    path.write_bytes(payload)
    return editing.import_source(
        path, "下载原件.mp4", hashlib.sha256(payload).hexdigest(),
        SOURCE_ASSET_ID, "import-source",
    ), path, payload


def project_with_plan(editing, tmp_path):
    source, _path, _payload = imported(editing, tmp_path)
    project = editing.create_project(source["id"], "中间编辑", "create-project")
    draft = editing.update_draft(project["id"], 1, recipe(), "save-draft")
    plan = editing.create_plan(project["id"], draft["version"], "create-plan")
    return source, project, draft, plan


def assert_no_paths(value):
    if isinstance(value, dict):
        assert not any("path" in key.lower() for key in value)
        for child in value.values():
            assert_no_paths(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_paths(child)


def test_editing_root_is_a_download_sibling():
    assert default_editing_root(Path("C:/data")) == Path("C:/data-edits")


def test_concurrent_imports_of_one_download_asset_converge_without_503(
    editing, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "concurrent.mp4"
    payload = b"one immutable source"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    barrier = Barrier(2)
    original_copy = editing._copy_and_hash

    def synchronized_copy(source, destination, maximum):
        result = original_copy(source, destination, maximum)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(editing, "_copy_and_hash", synchronized_copy)

    def run(key: str):
        return editing.import_source(
            path,
            path.name,
            digest,
            SOURCE_ASSET_ID,
            key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("concurrent-a", "concurrent-b")))

    assert results[0]["id"] == results[1]["id"]
    assert len(editing.sources()) == 1
    assert len(list(editing.source_root.glob("*.mp4"))) == 1


def test_exclusive_recovery_removes_only_strict_unregistered_media(
    editing, tmp_path
):
    source, _original, _payload = imported(editing, tmp_path)
    registered = editing.source_path(source["id"])
    orphan_source = editing.source_root / ("b" * 32 + ".mp4")
    orphan_asset = editing.asset_root / ("c" * 32 + ".png")
    unknown = editing.asset_root / "keep-this-unknown-file.txt"
    orphan_source.write_bytes(b"orphan source")
    orphan_asset.write_bytes(b"orphan asset")
    unknown.write_bytes(b"unknown")

    editing.recover_interrupted(cleanup_orphans=True)

    assert registered.is_file()
    assert not orphan_source.exists()
    assert not orphan_asset.exists()
    assert unknown.read_bytes() == b"unknown"


def test_exclusive_recovery_preserves_registered_case_only_media_names(
    editing, tmp_path
):
    source, project, _draft, plan = project_with_plan(editing, tmp_path)
    editing.confirm_plan(plan["id"])
    claim = editing.claim_next_plan()
    assert claim is not None
    output = editing.output_dir_for_plan(plan["id"], claim["claim_token"])
    rendered = output / "segment-001.mp4"
    payload = b"registered derived output"
    rendered.write_bytes(payload)
    ready = editing.complete_plan(
        plan["id"],
        claim["claim_token"],
        RenderResult(
            "ready",
            "render_complete",
            (
                RenderAsset(
                    "segment",
                    rendered,
                    rendered.name,
                    "video/mp4",
                    ordinal=1,
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
            ),
        ),
    )
    asset = ready["assets"][0]
    source_path = editing.source_root / f"{source['id']}.mp4"
    asset_path = editing.asset_root / f"{asset['id']}.mp4"
    case_source = source_path.with_suffix(".MP4")
    case_asset = asset_path.with_suffix(".MP4")
    source_path.rename(case_source)
    asset_path.rename(case_asset)

    editing.recover_interrupted(cleanup_orphans=True)

    assert case_source.read_bytes() == b"immutable download original"
    assert case_asset.read_bytes() == payload
    assert editing.source(source["id"])["sha256"] == source["sha256"]
    assert editing.asset(asset["id"])["sha256"] == asset["sha256"]


def test_exclusive_recovery_removes_ready_plan_staging_left_after_commit(
    editing, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    _source, _project, _draft, plan = project_with_plan(editing, tmp_path)
    editing.confirm_plan(plan["id"])
    claim = editing.claim_next_plan()
    assert claim is not None
    output = editing.output_dir_for_plan(plan["id"], claim["claim_token"])
    rendered = output / "segment-001.mp4"
    payload = b"committed output"
    rendered.write_bytes(payload)
    unknown_plan = editing.staging_root / "keep-unknown-plan"
    unknown_plan.mkdir()
    (unknown_plan / "keep.txt").write_bytes(b"unknown")
    strict_plan_unknown_claim = editing.staging_root / ("d" * 32)
    strict_plan_unknown_claim.mkdir()
    (strict_plan_unknown_claim / "keep-unknown-claim").mkdir()
    unsafe_plan = editing.staging_root / ("e" * 32)
    unsafe_plan.write_bytes(b"not a directory")
    strict_plan_unsafe_claim = editing.staging_root / ("f" * 32)
    strict_plan_unsafe_claim.mkdir()
    unsafe_claim = strict_plan_unsafe_claim / ("a" * 32)
    unsafe_claim.write_bytes(b"not a directory")

    original_remove = editing._remove_output_dir
    monkeypatch.setattr(editing, "_remove_output_dir", lambda _path: None)
    ready = editing.complete_plan(
        plan["id"],
        claim["claim_token"],
        RenderResult(
            "ready",
            "render_complete",
            (
                RenderAsset(
                    "segment",
                    rendered,
                    rendered.name,
                    "video/mp4",
                    ordinal=1,
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
            ),
        ),
    )
    assert ready["state"] == "ready"
    assert output.is_dir()
    monkeypatch.setattr(editing, "_remove_output_dir", original_remove)

    editing.recover_interrupted(cleanup_orphans=True)

    assert not output.exists()
    assert (unknown_plan / "keep.txt").read_bytes() == b"unknown"
    assert (strict_plan_unknown_claim / "keep-unknown-claim").is_dir()
    assert unsafe_plan.read_bytes() == b"not a directory"
    assert unsafe_claim.read_bytes() == b"not a directory"
    assert editing.asset_path(ready["assets"][0]["id"]).read_bytes() == payload


def test_orphan_staging_scan_preserves_active_claim(editing, tmp_path):
    _source, _project, _draft, plan = project_with_plan(editing, tmp_path)
    editing.confirm_plan(plan["id"])
    claim = editing.claim_next_plan()
    assert claim is not None
    output = editing.output_dir_for_plan(plan["id"], claim["claim_token"])
    partial = output / "partial.mp4"
    partial.write_bytes(b"active render")

    editing._cleanup_orphan_media()

    assert partial.read_bytes() == b"active render"
    assert editing.plan(plan["id"])["state"] == "running"


def test_source_is_copied_rehashed_and_public_records_hide_paths(editing, tmp_path):
    source, original, payload = imported(editing, tmp_path)

    original.write_bytes(b"changed after import")
    assert editing.source_path(source["id"]).read_bytes() == payload
    assert editing.import_source(
        tmp_path / "other.mp4", "下载原件.mp4", source["sha256"],
        SOURCE_ASSET_ID, "import-source",
    ) == source
    assert_no_paths(source)
    assert_no_paths(editing.status())


def test_import_rejects_hash_mismatch_and_noncanonical_download_id(editing, tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"payload")
    with pytest.raises(EditingError, match="source_hash_mismatch"):
        editing.import_source(path, path.name, "0" * 64, SOURCE_ASSET_ID)
    with pytest.raises(EditingError, match="invalid_source_asset_id"):
        editing.import_source(
            path, path.name, hashlib.sha256(b"payload").hexdigest(), SOURCE_ASSET_ID.upper()
        )
    assert editing.sources() == []


def test_drafts_use_expected_version_and_plans_freeze_the_reviewed_version(editing, tmp_path):
    source, project, draft, plan = project_with_plan(editing, tmp_path)

    assert project["current_version"] == 1
    assert draft["version"] == 2
    assert plan["state"] == "review"
    assert plan["recipe"] == draft["recipe"]
    assert editing.create_plan(project["id"], 2, "create-plan") == plan
    with pytest.raises(EditingError, match="idempotency_conflict"):
        editing.create_plan(project["id"], 2, "save-draft")
    with pytest.raises(EditingError, match="draft_version_conflict"):
        editing.update_draft(project["id"], 1, recipe(), "stale-save")

    newer = editing.update_draft(
        project["id"], 2,
        recipe(segments=[{"start_ms": 5, "end_ms": 500, "label": "new"}]),
        "newer-save",
    )
    assert newer["version"] == 3
    assert editing.plan(plan["id"])["recipe"] == draft["recipe"]
    current = editing.project(project["id"])
    assert current["source_name"] == source["name"]
    assert current["source_size"] == source["size"]
    assert current["source_sha256"] == source["sha256"]
    assert editing.project_source_path(project["id"]).read_bytes()
    assert_no_paths(current)


def test_ai_block_is_distinct_from_review_and_confirm_is_explicit(editing, tmp_path):
    source, _path, _payload = imported(editing, tmp_path)
    project = editing.create_project(source["id"], "AI", "ai-project")
    blocked = recipe(translation={
        "enabled": True, "source_language": "auto", "target_language": "en",
        "provider": "openai", "model": "model", "state": "blocked",
    })
    draft = editing.update_draft(project["id"], 1, blocked, "ai-draft")
    plan = editing.create_plan(project["id"], draft["version"], "ai-plan")

    with pytest.raises(EditingError, match="ai_operation_blocked"):
        editing.confirm_plan(plan["id"])
    assert editing.plan(plan["id"])["state"] == "review"


def test_claim_completion_fencing_and_immutable_assets(editing, tmp_path):
    _source, project, _draft, plan = project_with_plan(editing, tmp_path)
    assert editing.confirm_plan(plan["id"])["state"] == "queued"
    assert editing.confirm_plan(plan["id"])["state"] == "queued"
    claim = editing.claim_next_plan()
    assert claim is not None and claim["state"] == "running"
    assert "claim_token" in claim
    output = editing.output_dir_for_plan(plan["id"], claim["claim_token"])
    rendered = output / "片段-01.mp4"
    payload = b"derived output"
    rendered.write_bytes(payload)
    output_hash = hashlib.sha256(payload).hexdigest()
    result = RenderResult("ready", "render_complete", (
        RenderAsset(
            "segment", rendered, rendered.name, "video/mp4", ordinal=1,
            size_bytes=len(payload), sha256=output_hash, duration_ms=1200,
            width=1920, height=1080, container="mp4", video_codec="h264", audio_codec="aac",
        ),
    ))
    with pytest.raises(EditingError, match="stale_render_claim"):
        editing.complete_plan(plan["id"], "0" * 32, result)

    ready = editing.complete_plan(plan["id"], claim["claim_token"], result)
    assert ready["state"] == "ready"
    assert ready["assets"][0]["duration_ms"] == 1200
    assert ready["assets"][0]["width"] == 1920
    assert editing.asset_path(ready["assets"][0]["id"]).read_bytes() == payload
    assert editing.assets(project_id=project["id"]) == ready["assets"]
    assert_no_paths(ready)


def test_cancel_running_has_no_unknown_state_and_retry_is_reviewable(editing, tmp_path):
    _source, _project, _draft, plan = project_with_plan(editing, tmp_path)
    editing.confirm_plan(plan["id"])
    claim = editing.claim_next_plan()
    assert claim is not None
    assert editing.cancel_plan(plan["id"])["state"] == "canceling"
    assert editing.plan_cancellation_requested(plan["id"], claim["claim_token"])
    canceled = editing.fail_plan(plan["id"], claim["claim_token"], "canceled", canceled=True)
    assert canceled["state"] == "canceled"
    assert all(item["state"] != "unknown" for item in editing.plans())

    retry = editing.retry_plan(plan["id"], "retry-plan")
    assert retry["state"] == "review" and retry["retry_of"] == plan["id"]
    assert editing.retry_plan(plan["id"], "retry-plan") == retry


def test_restart_fails_running_and_revokes_queued_confirmation(editing, tmp_path):
    _source, project, draft, first = project_with_plan(editing, tmp_path)
    second = editing.create_plan(project["id"], draft["version"], "second-plan")
    editing.confirm_plan(first["id"])
    editing.confirm_plan(second["id"])
    claimed = editing.claim_next_plan()
    assert claimed is not None
    interrupted_output = editing.output_dir_for_plan(
        claimed["id"], claimed["claim_token"]
    )
    (interrupted_output / "partial.mp4").write_bytes(b"partial")

    restarted = EditingService(editing.root)
    states = {row["id"]: row for row in restarted.plans()}
    assert states[claimed["id"]]["state"] == "failed"
    assert states[claimed["id"]]["code"] == "render_interrupted"
    other = second["id"] if claimed["id"] == first["id"] else first["id"]
    assert states[other]["state"] == "review"
    assert states[other]["code"] == "restart_confirmation_required"
    assert "unknown" not in {row["state"] for row in states.values()}
    assert not interrupted_output.exists()


def test_database_enforces_render_recipe_immutability(editing, tmp_path):
    _source, _project, _draft, plan = project_with_plan(editing, tmp_path)
    with sqlite3.connect(editing.database_path) as db:
        before = db.execute("SELECT recipe FROM render_plans WHERE id=?", (plan["id"],)).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable render plan"):
            db.execute("UPDATE render_plans SET recipe=? WHERE id=?", (json.dumps({}), plan["id"]))
    assert editing.plan(plan["id"])["recipe"] == json.loads(before)
    assert before
