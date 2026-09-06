from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .uploads.backup import (
    UploadBackupError,
    create_upload_backup,
    restore_upload_backup,
)


class _UploadBackupArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _UploadBackupArgumentError("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="video-upload-backup",
        description=(
            "Create or restore a verified, secret-free Open Flame upload backup. "
            "Stop every Open Flame app and upload operation using the relevant "
            "upload root before create or restore."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser(
        "create",
        help="create from an upload root with all app activity stopped",
    )
    create.add_argument("--source-upload-root", type=Path, required=True)
    create.add_argument("--backup-target", type=Path, required=True)

    restore = subcommands.add_parser(
        "restore",
        help="restore into a nonexistent root with all app activity stopped",
    )
    restore.add_argument("--backup-root", type=Path, required=True)
    restore.add_argument("--restore-upload-root", type=Path, required=True)
    return parser


def _write_json(payload: dict[str, object], *, stream) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=stream,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
    except _UploadBackupArgumentError:
        _write_json(
            {
                "detail": "invalid command arguments",
                "error": "invalid_arguments",
                "status": "error",
            },
            stream=sys.stderr,
        )
        return 2

    try:
        if arguments.command == "create":
            result = create_upload_backup(
                source_root=arguments.source_upload_root,
                backup_target=arguments.backup_target,
            )
            payload = {
                "backup_root": str(result.backup_root),
                "file_count": result.file_count,
                "manifest_sha256": result.manifest_sha256,
                "operation": "create",
                "schema_version": result.schema_version,
                "status": "ok",
                "total_bytes": result.total_bytes,
            }
        else:
            result = restore_upload_backup(
                backup_root=arguments.backup_root,
                restore_root=arguments.restore_upload_root,
            )
            payload = {
                "database_path": str(result.database_path),
                "file_count": result.file_count,
                "manifest_sha256": result.manifest_sha256,
                "operation": "restore",
                "restore_root": str(result.restore_root),
                "schema_version": result.schema_version,
                "status": "ok",
                "total_bytes": result.total_bytes,
            }
    except UploadBackupError as exc:
        _write_json(
            {
                "detail": str(exc),
                "error": "upload_backup_failed",
                "status": "error",
            },
            stream=sys.stderr,
        )
        return 2
    except Exception:
        _write_json(
            {
                "detail": "unexpected upload backup failure",
                "error": "internal_error",
                "status": "error",
            },
            stream=sys.stderr,
        )
        return 1

    _write_json(payload, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
