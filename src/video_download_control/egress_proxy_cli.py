from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from .security.forward_proxy import (
    ControlledForwardProxy,
    ForwardProxyLimits,
    ProxyAuditEvent,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Policy-controlled HTTP forward proxy over a Unix socket"
    )
    parser.add_argument("--unix-socket", type=Path, required=True)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="operator-approved hostname or suffix; repeat for each entry",
    )
    parser.add_argument(
        "--allowed-host-file",
        type=Path,
        help=(
            "absolute UTF-8 policy file with one hostname/suffix per line; "
            "blank lines and # comments are ignored"
        ),
    )
    parser.add_argument("--max-connections", type=int, default=64)
    parser.add_argument("--max-upload-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument(
        "--max-download-bytes", type=int, default=512 * 1024 * 1024
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--idle-timeout", type=float, default=30.0)
    parser.add_argument("--total-timeout", type=float, default=300.0)
    return parser


def _load_allowed_hosts(args: argparse.Namespace) -> tuple[str, ...]:
    hosts = list(args.allowed_host)
    policy_path = args.allowed_host_file
    if policy_path is not None:
        if not policy_path.is_absolute():
            raise ValueError("allowed-host file path must be absolute")
        try:
            info = policy_path.lstat()
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            attributes = getattr(info, "st_file_attributes", 0)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or (reparse and attributes & reparse)
                or info.st_nlink != 1
                or info.st_size > 16 * 1024
                or (os.name == "posix" and info.st_mode & 0o222)
            ):
                raise ValueError("allowed-host file is not a bounded plain file")
            with policy_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (
                    opened.st_dev != info.st_dev
                    or opened.st_ino != info.st_ino
                    or opened.st_size != info.st_size
                    or opened.st_mtime_ns != info.st_mtime_ns
                ):
                    raise ValueError("allowed-host file changed before reading")
                payload = handle.read(16 * 1024 + 1)
                after = os.fstat(handle.fileno())
            if len(payload) > 16 * 1024 or (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                raise ValueError("allowed-host file changed while reading")
            text = payload.decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise ValueError("allowed-host file could not be read safely") from exc
        hosts.extend(
            line
            for raw_line in text.splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        )
    if not hosts or len(hosts) > 128:
        raise ValueError("one to 128 allowed hosts are required")
    if len(set(hosts)) != len(hosts):
        raise ValueError("allowed hosts must be unique")
    return tuple(hosts)


def _emit_audit(event: ProxyAuditEvent) -> None:
    print(
        json.dumps(
            {
                "event": "egress_proxy_rejected",
                "reason": event.reason,
                "policy_host": event.host,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


async def _serve(args: argparse.Namespace, allowed_hosts: tuple[str, ...]) -> None:
    proxy = ControlledForwardProxy(
        allowed_hosts=allowed_hosts,
        limits=ForwardProxyLimits(
            max_connections=args.max_connections,
            max_upload_bytes=args.max_upload_bytes,
            max_download_bytes=args.max_download_bytes,
            connect_timeout_seconds=args.connect_timeout,
            idle_timeout_seconds=args.idle_timeout,
            total_timeout_seconds=args.total_timeout,
        ),
        audit=_emit_audit,
    )
    await proxy.serve_unix(args.unix_socket, mode=0o660)


def main() -> None:
    args = build_parser().parse_args()
    try:
        allowed_hosts = _load_allowed_hosts(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        asyncio.run(_serve(args, allowed_hosts))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
