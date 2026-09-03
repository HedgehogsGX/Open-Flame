"""CLI for the authenticated egress-side short-link transport service."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from .short_link_transport import (
    FileReplayStore,
    ShortLinkEgressService,
    ShortLinkTransportAuditEvent,
    ShortLinkTransportLimits,
    load_shared_key,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authenticated single-hop short-link egress service"
    )
    parser.add_argument("--unix-socket", type=Path, required=True)
    parser.add_argument("--shared-key-file", type=Path, required=True)
    parser.add_argument("--replay-directory", type=Path, required=True)
    parser.add_argument("--max-connections", type=int, default=16)
    parser.add_argument("--max-addresses", type=int, default=16)
    parser.add_argument("--max-header-bytes", type=int, default=32 * 1024)
    parser.add_argument("--max-body-bytes", type=int, default=8 * 1024)
    parser.add_argument("--max-timeout", type=float, default=10.0)
    parser.add_argument("--max-validity", type=float, default=20.0)
    parser.add_argument("--clock-skew", type=float, default=2.0)
    parser.add_argument("--frame-timeout", type=float, default=5.0)
    return parser


def _emit_audit(event: ShortLinkTransportAuditEvent) -> None:
    print(
        json.dumps(
            {
                "event": "short_link_egress_rejected",
                "reason": event.reason,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


def build_service(
    arguments: argparse.Namespace, *, shared_key: bytes
) -> ShortLinkEgressService:
    """Build the egress server without starting it, for supervisors/tests."""

    return ShortLinkEgressService(
        shared_key=shared_key,
        replay_store=FileReplayStore(arguments.replay_directory),
        limits=ShortLinkTransportLimits(
            max_connections=arguments.max_connections,
            max_addresses=arguments.max_addresses,
            max_response_header_bytes=arguments.max_header_bytes,
            max_body_bytes=arguments.max_body_bytes,
            max_timeout_seconds=arguments.max_timeout,
            max_validity_seconds=arguments.max_validity,
            clock_skew_seconds=arguments.clock_skew,
            frame_timeout_seconds=arguments.frame_timeout,
        ),
        audit=_emit_audit,
    )


async def _serve(service: ShortLinkEgressService, socket_path: Path) -> None:
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    tasks: tuple[asyncio.Task[object], ...] = ()
    try:
        for candidate in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(candidate, shutdown.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed_signals.append(candidate)

        server = await service.start_unix(socket_path, mode=0o600)
        serving = asyncio.create_task(server.serve_forever())
        stopping = asyncio.create_task(shutdown.wait())
        tasks = (serving, stopping)
        completed, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if serving in completed:
            await serving
    finally:
        for candidate in installed_signals:
            loop.remove_signal_handler(candidate)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        shared_key = load_shared_key(arguments.shared_key_file)
        service = build_service(arguments, shared_key=shared_key)
        asyncio.run(_serve(service, arguments.unix_socket))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 - process boundary emits fixed diagnostics
        print("short-link egress configuration failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_parser",
    "build_service",
    "load_shared_key",
    "main",
]
