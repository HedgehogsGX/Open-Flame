from __future__ import annotations

import sqlite3

import pytest

from video_download_control.editing.schema import (
    SCHEMA_VERSION,
    EditingSchemaError,
    ensure_editing_schema,
    validate_editing_schema,
)


def test_fresh_schema1_is_exact_and_idempotent(tmp_path):
    path = tmp_path / "editing.sqlite3"

    ensure_editing_schema(path)
    validate_editing_schema(path)
    before = path.read_bytes()
    ensure_editing_schema(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert SCHEMA_VERSION == 1
        assert db.execute("SELECT version FROM metadata").fetchall() == [(1,)]
        assert db.execute("PRAGMA application_id").fetchone() == (0x4F464544,)
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        } == {
            "metadata", "sources", "projects", "drafts", "render_plans", "assets", "requests"
        }
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(assets)")] == [
            "id", "plan_id", "kind", "name", "suffix", "mime_type", "size", "sha256",
            "ordinal", "duration_ms", "width", "height", "container", "video_codec",
            "audio_codec", "created_at",
        ]
        assert [row[1] for row in db.execute("PRAGMA table_xinfo(render_plans)")][-1] == "retry_of"


@pytest.mark.parametrize("mutation", ["version", "table", "index"])
def test_schema_validation_fails_closed_on_shape_changes(tmp_path, mutation):
    path = tmp_path / "editing.sqlite3"
    ensure_editing_schema(path)
    with sqlite3.connect(path) as db:
        if mutation == "version":
            db.execute("UPDATE metadata SET version=2")
        elif mutation == "table":
            db.execute("CREATE TABLE surprise(value TEXT)")
        else:
            db.execute("DROP INDEX plans_state")

    with pytest.raises(EditingSchemaError):
        validate_editing_schema(path)
    with pytest.raises(EditingSchemaError):
        ensure_editing_schema(path)


def test_schema_refuses_a_database_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    path = tmp_path / "editing.sqlite3"
    ensure_editing_schema(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(EditingSchemaError):
        ensure_editing_schema(path)
