from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .toolchain import (
    ToolchainError,
    inspect_toolchain,
    install_toolchain,
    run_offline_smoke,
    verify_toolchain,
)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and verify the pinned local media toolchain."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "verify", "smoke"):
        child = subparsers.add_parser(command)
        child.add_argument("--tool-root", required=True, type=_absolute_path)
    install = subparsers.add_parser("install")
    install.add_argument("--tool-root", required=True, type=_absolute_path)
    install.add_argument(
        "--artifact-cache",
        type=_absolute_path,
        help="Optional absolute directory containing the exact locked downloads.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "install":
            result = install_toolchain(
                arguments.tool_root,
                artifact_cache=arguments.artifact_cache,
            ).to_dict()
        elif arguments.command == "status":
            result = inspect_toolchain(arguments.tool_root).to_dict()
        elif arguments.command == "verify":
            verified = verify_toolchain(arguments.tool_root)
            result = {
                "state": "verified",
                "bundle_id": verified.bundle_id,
                "yt_dlp_version": verified.yt_dlp_version,
                "ffmpeg_version": verified.ffmpeg_version,
                "ffprobe_version": verified.ffprobe_version,
                "offline_smoke_passed": verified.offline_smoke_passed,
            }
        else:
            verified = run_offline_smoke(arguments.tool_root)
            status = inspect_toolchain(arguments.tool_root)
            if status.state != "ready":
                raise ToolchainError(
                    status.detail_code,
                    "toolchain changed after the offline smoke check",
                )
            result = {"bundle_id": verified.bundle_id, **status.to_dict()}
    except (OSError, ToolchainError) as error:
        code = (
            error.code if isinstance(error, ToolchainError) else "toolchain_unavailable"
        )
        print(
            json.dumps({"state": "error", "detail_code": code}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
