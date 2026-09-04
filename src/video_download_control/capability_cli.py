from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Sequence

from .capability_evidence import (
    CapabilityEvidenceRepository,
    CapabilityGovernanceError,
)
from .database import Database
from .validation import ValidationManifestError


MAX_EVIDENCE_INPUT_BYTES = 4 * 1024 * 1024


class _CapabilityArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _CapabilityArgumentError("invalid_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="video-download-capabilities",
        description="Local append-only Stage 0 evidence and decision administrator.",
    )
    parser.add_argument("--database-path", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser("import")
    import_command.add_argument("--manifest", required=True)
    import_command.add_argument("--results", required=True)
    import_command.add_argument("--environment-key", required=True)

    for action in ("approve", "revoke"):
        command = commands.add_parser(action)
        command.add_argument("--evidence-id", required=True)
        command.add_argument("--expected-revision", required=True, type=int)
        command.add_argument("--reason-code", required=True)

    listing = commands.add_parser("list")
    listing.add_argument(
        "--kind", choices=("evidence", "decisions"), default="decisions"
    )
    listing.add_argument("--limit", type=int, default=200)

    history = commands.add_parser("history")
    history.add_argument("--identity-key", required=True)
    history.add_argument("--limit", type=int, default=200)
    return parser


def _absolute_normalized_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise CapabilityGovernanceError("path_not_absolute", exit_code=2)
    return path


def _plain_file_identity(
    path: Path, *, require_single_link: bool = False
) -> tuple[int, int]:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise CapabilityGovernanceError("input_unavailable", exit_code=6) from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or bool(
            reparse and attributes & reparse
        ):
            raise CapabilityGovernanceError("linked_path_rejected", exit_code=2)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CapabilityGovernanceError("input_unavailable", exit_code=6) from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if not stat.S_ISREG(info.st_mode) or bool(reparse and attributes & reparse):
        raise CapabilityGovernanceError("plain_file_required", exit_code=2)
    if require_single_link and getattr(info, "st_nlink", 1) != 1:
        raise CapabilityGovernanceError("hard_link_rejected", exit_code=2)
    return int(info.st_dev), int(info.st_ino)


def _require_plain_existing_file(path: Path) -> None:
    _plain_file_identity(path)


def _read_input(path: Path) -> bytes:
    _require_plain_existing_file(path)
    try:
        size = path.stat().st_size
        if size > MAX_EVIDENCE_INPUT_BYTES:
            raise CapabilityGovernanceError("evidence_input_too_large", exit_code=2)
        content = path.read_bytes()
    except CapabilityGovernanceError:
        raise
    except OSError as exc:
        raise CapabilityGovernanceError("input_unavailable", exit_code=6) from exc
    if len(content) != size or len(content) > MAX_EVIDENCE_INPUT_BYTES:
        raise CapabilityGovernanceError("evidence_input_changed", exit_code=6)
    return content


def _database_and_repository(raw_path: str) -> CapabilityEvidenceRepository:
    path = _absolute_normalized_path(raw_path)
    expected_identity = _plain_file_identity(path, require_single_link=True)

    def validate_database_path(candidate: Path) -> None:
        identity = _plain_file_identity(
            candidate, require_single_link=True
        )
        if identity != expected_identity:
            raise CapabilityGovernanceError(
                "database_path_changed", exit_code=6
            )

    database = Database(path, path_validator=validate_database_path)
    ready, _ = database.readiness()
    if not ready:
        raise CapabilityGovernanceError("database_not_ready", exit_code=6)
    return CapabilityEvidenceRepository(database)


def _dispatch(
    args: argparse.Namespace, repository: CapabilityEvidenceRepository
) -> dict[str, object]:
    if args.command == "import":
        manifest = _read_input(_absolute_normalized_path(args.manifest))
        results = _read_input(_absolute_normalized_path(args.results))
        return repository.import_csv_bundle(
            manifest_content=manifest,
            results_content=results,
            environment=args.environment_key,
        ).to_public_dict()
    if args.command == "approve":
        return repository.approve(
            evidence_id=args.evidence_id,
            expected_revision=args.expected_revision,
            reason_code=args.reason_code,
        ).to_public_dict()
    if args.command == "revoke":
        return repository.revoke(
            evidence_id=args.evidence_id,
            expected_revision=args.expected_revision,
            reason_code=args.reason_code,
        ).to_public_dict()
    if args.command == "list":
        if args.kind == "evidence":
            records = repository.list_evidence(limit=args.limit)
        else:
            records = repository.list_current_decisions(limit=args.limit)
        return {
            "status": "ok",
            "operation": "list",
            "kind": args.kind,
            "count": len(records),
            "records": records,
        }
    if args.command == "history":
        records = repository.history(
            identity_key=args.identity_key,
            limit=args.limit,
        )
        return {
            "status": "ok",
            "operation": "history",
            "count": len(records),
            "records": records,
        }
    raise AssertionError("unreachable capability command")


def _emit_error(error_code: str) -> None:
    print(
        json.dumps(
            {"status": "error", "error_code": error_code},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        repository = _database_and_repository(args.database_path)
        payload = _dispatch(args, repository)
    except _CapabilityArgumentError:
        _emit_error("invalid_arguments")
        return 2
    except CapabilityGovernanceError as exc:
        _emit_error(exc.error_code)
        return exc.exit_code
    except ValidationManifestError:
        _emit_error("invalid_validation_input")
        return 2
    except (OSError, RuntimeError, sqlite3.Error):
        _emit_error("database_or_io_unavailable")
        return 6
    except Exception:  # noqa: BLE001 - sanitize the final process boundary
        _emit_error("internal_error")
        return 70
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_EVIDENCE_INPUT_BYTES", "build_parser", "main"]
