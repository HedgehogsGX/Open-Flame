from __future__ import annotations

import math

import pytest

from video_download_control.relay_cli import build_parser
from video_download_control.security.unix_relay import UnixRelayLimits


def test_relay_cli_requires_explicit_upstream_socket() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_relay_cli_defaults_are_bounded() -> None:
    args = build_parser().parse_args(
        ["--upstream-socket", "/run/video-download/proxy.sock"]
    )
    limits = UnixRelayLimits(
        max_connections=args.max_connections,
        max_client_to_upstream_bytes=args.max_upload_bytes,
        max_upstream_to_client_bytes=args.max_download_bytes,
        connect_timeout_seconds=args.connect_timeout,
        idle_timeout_seconds=args.idle_timeout,
        total_timeout_seconds=args.total_timeout,
    )
    assert args.listen_host == "127.0.0.1"
    assert args.listen_port == 18080
    assert limits.max_connections == 16
    assert all(
        math.isfinite(value)
        for value in (
            limits.connect_timeout_seconds,
            limits.idle_timeout_seconds,
            limits.total_timeout_seconds,
        )
    )
