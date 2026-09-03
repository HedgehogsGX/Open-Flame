from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .security.unix_relay import UnixRelayLimits, UnixSocketRelay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loopback TCP to private Unix-socket relay"
    )
    parser.add_argument("--upstream-socket", type=Path, required=True)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18080)
    parser.add_argument("--max-connections", type=int, default=16)
    parser.add_argument(
        "--max-upload-bytes", type=int, default=64 * 1024 * 1024
    )
    parser.add_argument(
        "--max-download-bytes", type=int, default=512 * 1024 * 1024
    )
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--idle-timeout", type=float, default=30.0)
    parser.add_argument("--total-timeout", type=float, default=300.0)
    return parser


async def _serve(args: argparse.Namespace) -> None:
    relay = UnixSocketRelay(
        args.upstream_socket,
        limits=UnixRelayLimits(
            max_connections=args.max_connections,
            max_client_to_upstream_bytes=args.max_upload_bytes,
            max_upstream_to_client_bytes=args.max_download_bytes,
            connect_timeout_seconds=args.connect_timeout,
            idle_timeout_seconds=args.idle_timeout,
            total_timeout_seconds=args.total_timeout,
        ),
    )
    await relay.serve_forever(args.listen_host, args.listen_port)


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
