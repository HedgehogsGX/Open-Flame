from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Sequence

from .credentials import CredentialProfileError, CredentialRepository
from .database import Database
from .domain import Platform


def _absolute_database_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise argparse.ArgumentTypeError(
            "database path must be explicit, absolute, and normalized"
        )
    return path


def _platform(raw: str) -> Platform:
    try:
        return Platform(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("unsupported platform") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local administrator for opaque credential profiles; never accepts "
            "Cookie contents or Cookie file paths"
        )
    )
    parser.add_argument(
        "--database-path",
        required=True,
        type=_absolute_database_path,
        help="existing control.sqlite3 path",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="register an opaque reference")
    register.add_argument("--platform", required=True, type=_platform)
    register.add_argument("--name", required=True)
    register.add_argument("--secret-ref", required=True)
    register.add_argument("--expires-at")

    listing = commands.add_parser("list", help="list profile metadata")
    listing.add_argument("--platform", type=_platform)
    listing.add_argument("--include-disabled", action="store_true")

    assign = commands.add_parser("assign", help="assign one profile before claim")
    assign.add_argument("--profile-id", required=True)
    scope = assign.add_mutually_exclusive_group(required=True)
    scope.add_argument("--job-id", action="append", default=[])
    scope.add_argument("--batch-id")

    clear = commands.add_parser("clear", help="clear a queued job assignment")
    clear.add_argument("--job-id", action="append", required=True)

    disable = commands.add_parser(
        "disable", help="disable a profile and request active job cancellation"
    )
    disable.add_argument("--profile-id", required=True)
    return parser


def _require_existing_plain_database(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            component = current.lstat()
        except OSError as exc:
            raise CredentialProfileError("database file does not exist") from exc
        component_reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        component_attributes = getattr(component, "st_file_attributes", 0)
        if stat.S_ISLNK(component.st_mode) or bool(
            component_reparse and component_attributes & component_reparse
        ):
            raise CredentialProfileError("database path may not contain links")
    try:
        info = path.lstat()
    except OSError as exc:
        raise CredentialProfileError("database file does not exist") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(info.st_mode)
        or bool(reparse and attributes & reparse)
    ):
        raise CredentialProfileError("database must be a plain regular file")


def _dispatch(args: argparse.Namespace, repository: CredentialRepository):
    if args.command == "register":
        return repository.register(
            platform=args.platform,
            name=args.name,
            secret_ref=args.secret_ref,
            expires_at=args.expires_at,
        )
    if args.command == "list":
        return {
            "profiles": repository.list(
                platform=args.platform,
                include_disabled=args.include_disabled,
            )
        }
    if args.command == "assign":
        return repository.assign(
            profile_id=args.profile_id,
            job_ids=args.job_id,
            batch_id=args.batch_id,
        )
    if args.command == "clear":
        return repository.clear_assignment(job_ids=args.job_id)
    if args.command == "disable":
        return repository.disable(profile_id=args.profile_id)
    raise AssertionError("unreachable credential command")


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        _require_existing_plain_database(args.database_path)
        database = Database(args.database_path)
        database.initialize()
        payload = _dispatch(args, CredentialRepository(database))
    except (CredentialProfileError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "main"]
