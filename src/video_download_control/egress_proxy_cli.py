from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .security.egress import load_allowed_hosts
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
    return load_allowed_hosts(args.allowed_host, policy_path=args.allowed_host_file)


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
