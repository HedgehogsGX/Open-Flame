from __future__ import annotations

import json
import tomllib
from pathlib import Path

from video_download_control import upload_backup_cli
from video_download_control.upload_backup_cli import main
from video_download_control.uploads.schema import (
    ensure_upload_schema,
    validate_upload_schema,
)


def _make_upload_root(tmp_path: Path) -> Path:
    root = tmp_path / "upload-root"
    (root / "media").mkdir(parents=True)
    (root / "assets").mkdir()
    ensure_upload_schema(root / "uploads.sqlite3")
    (root / ".worker.lock").write_bytes(b"0")
    return root


def test_cli_creates_and_restores_an_upload_backup(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = _make_upload_root(tmp_path)
    backup_root = tmp_path / "backup"

    assert main(
        [
            "create",
            "--source-upload-root",
            str(source_root),
            "--backup-target",
            str(backup_root),
        ]
    ) == 0
    captured = capsys.readouterr()
    created = json.loads(captured.out)
    assert captured.err == ""
    assert created == {
        "backup_root": str(backup_root),
        "file_count": 2,
        "manifest_sha256": created["manifest_sha256"],
        "operation": "create",
        "schema_version": 4,
        "status": "ok",
        "total_bytes": created["total_bytes"],
    }
    assert len(created["manifest_sha256"]) == 64
    assert created["total_bytes"] > 0

    restore_root = tmp_path / "restore"
    assert main(
        [
            "restore",
            "--backup-root",
            str(backup_root),
            "--restore-upload-root",
            str(restore_root),
        ]
    ) == 0
    captured = capsys.readouterr()
    restored = json.loads(captured.out)
    assert captured.err == ""
    assert restored == {
        "database_path": str(restore_root / "uploads.sqlite3"),
        "file_count": 1,
        "manifest_sha256": created["manifest_sha256"],
        "operation": "restore",
        "restore_root": str(restore_root),
        "schema_version": 4,
        "status": "ok",
        "total_bytes": restored["total_bytes"],
    }
    assert restored["total_bytes"] > 0
    validate_upload_schema(restore_root / "uploads.sqlite3")


def test_cli_refuses_restore_over_an_existing_target_with_safe_json(
    tmp_path: Path,
    capsys,
) -> None:
    source_root = _make_upload_root(tmp_path)
    backup_root = tmp_path / "backup"
    assert main(
        [
            "create",
            "--source-upload-root",
            str(source_root),
            "--backup-target",
            str(backup_root),
        ]
    ) == 0
    capsys.readouterr()

    secret_component = "operator-secret-location"
    restore_root = tmp_path / secret_component
    restore_root.mkdir()
    assert main(
        [
            "restore",
            "--backup-root",
            str(backup_root),
            "--restore-upload-root",
            str(restore_root),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error == {
        "detail": "upload restore target already exists",
        "error": "upload_backup_failed",
        "status": "error",
    }
    assert secret_component not in captured.err


def test_cli_argument_errors_are_stable_json_and_do_not_echo_values(
    capsys,
) -> None:
    secret_component = "operator-secret-location"
    assert main(
        [
            "create",
            "--source-upload-root",
            secret_component,
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "detail": "invalid command arguments",
        "error": "invalid_arguments",
        "status": "error",
    }
    assert secret_component not in captured.err


def test_cli_masks_unexpected_exception_details(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    secret = "SECRET-CREDENTIAL-CANARY"

    def fail_create(**kwargs) -> None:
        del kwargs
        raise RuntimeError(secret)

    monkeypatch.setattr(upload_backup_cli, "create_upload_backup", fail_create)
    assert main(
        [
            "create",
            "--source-upload-root",
            str(tmp_path / "source"),
            "--backup-target",
            str(tmp_path / "backup"),
        ]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "detail": "unexpected upload backup failure",
        "error": "internal_error",
        "status": "error",
    }
    assert secret not in captured.err


def test_cli_help_requires_all_upload_activity_to_be_stopped() -> None:
    help_text = " ".join(upload_backup_cli.build_parser().format_help().split())
    assert "Stop every Open Flame app and upload operation" in help_text
    assert "before create or restore" in help_text


def test_project_registers_upload_backup_entry_point() -> None:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text("utf-8"))
    assert project["project"]["scripts"]["video-upload-backup"] == (
        "video_download_control.upload_backup_cli:main"
    )
