from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .backup import BackupRestoreError, create_backup, restore_backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-download-backup",
        description="Create or restore a verified Video Download Control backup.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser("create", help="create a new backup directory")
    create.add_argument("--source-data-root", type=Path, required=True)
    create.add_argument("--source-database", type=Path, required=True)
    create.add_argument("--backup-target", type=Path, required=True)

    restore = subcommands.add_parser(
        "restore", help="restore into a nonexistent independent data root"
    )
    restore.add_argument("--backup-root", type=Path, required=True)
    restore.add_argument("--restore-data-root", type=Path, required=True)
    restore.add_argument("--restore-database", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_backup(
                source_data_root=arguments.source_data_root,
                source_database_path=arguments.source_database,
                backup_target=arguments.backup_target,
            )
            payload = {
                "status": "ok",
                "operation": "create",
                "schema_version": result.schema_version,
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
                "manifest_sha256": result.manifest_sha256,
                "backup_root": str(result.backup_root),
            }
        else:
            result = restore_backup(
                backup_root=arguments.backup_root,
                restore_data_root=arguments.restore_data_root,
                restore_database_path=arguments.restore_database,
            )
            payload = {
                "status": "ok",
                "operation": "restore",
                "schema_version": result.schema_version,
                "file_count": result.file_count,
                "total_bytes": result.total_bytes,
                "manifest_sha256": result.manifest_sha256,
                "restore_root": str(result.restore_root),
                "database_path": str(result.database_path),
            }
    except BackupRestoreError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "backup_restore_failed",
                    "detail": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
