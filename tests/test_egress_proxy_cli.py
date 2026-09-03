from __future__ import annotations

import json
import math
import os

import pytest

from video_download_control.egress_proxy_cli import (
    _emit_audit,
    _load_allowed_hosts,
    build_parser,
)
from video_download_control.security.forward_proxy import (
    ForwardProxyLimits,
    ProxyAuditEvent,
)


def test_egress_proxy_cli_requires_socket_and_nonempty_policy() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    args = build_parser().parse_args(
        ["--unix-socket", "/run/video-download/proxy.sock"]
    )
    with pytest.raises(ValueError, match="required"):
        _load_allowed_hosts(args)


def test_egress_proxy_cli_defaults_are_bounded() -> None:
    args = build_parser().parse_args(
        [
            "--unix-socket",
            "/run/video-download/proxy.sock",
            "--allowed-host",
            "youtube.com",
            "--allowed-host",
            "googlevideo.com",
        ]
    )
    limits = ForwardProxyLimits(
        max_connections=args.max_connections,
        max_upload_bytes=args.max_upload_bytes,
        max_download_bytes=args.max_download_bytes,
        connect_timeout_seconds=args.connect_timeout,
        idle_timeout_seconds=args.idle_timeout,
        total_timeout_seconds=args.total_timeout,
    )
    assert args.allowed_host == ["youtube.com", "googlevideo.com"]
    assert _load_allowed_hosts(args) == ("youtube.com", "googlevideo.com")
    assert limits.max_connections == 64
    assert all(
        math.isfinite(value)
        for value in (
            limits.connect_timeout_seconds,
            limits.idle_timeout_seconds,
            limits.total_timeout_seconds,
        )
    )


def test_egress_proxy_cli_audit_is_sparse_json(capsys: pytest.CaptureFixture[str]) -> None:
    _emit_audit(ProxyAuditEvent(reason="blocked_host", host="youtube.com"))
    payload = json.loads(capsys.readouterr().err)
    assert payload == {
        "event": "egress_proxy_rejected",
        "reason": "blocked_host",
        "policy_host": "youtube.com",
    }


def test_allowed_host_file_is_bounded_and_combines_with_cli(tmp_path) -> None:
    policy = (tmp_path / "hosts.txt").resolve()
    policy.write_text(
        "# operator-reviewed\nexample.com\n\ncdn.example.com\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        policy.chmod(0o444)
    args = build_parser().parse_args(
        [
            "--unix-socket",
            "/tmp/proxy.sock",
            "--allowed-host",
            "media.example.com",
            "--allowed-host-file",
            str(policy),
        ]
    )

    assert _load_allowed_hosts(args) == (
        "media.example.com",
        "example.com",
        "cdn.example.com",
    )


def test_allowed_host_file_rejects_relative_duplicate_and_oversize(tmp_path) -> None:
    relative = build_parser().parse_args(
        [
            "--unix-socket",
            "/tmp/proxy.sock",
            "--allowed-host-file",
            "relative.txt",
        ]
    )
    with pytest.raises(ValueError, match="absolute"):
        _load_allowed_hosts(relative)

    duplicate_path = (tmp_path / "duplicate.txt").resolve()
    duplicate_path.write_text("example.com\n", encoding="utf-8")
    if os.name == "posix":
        duplicate_path.chmod(0o444)
    duplicate = build_parser().parse_args(
        [
            "--unix-socket",
            "/tmp/proxy.sock",
            "--allowed-host",
            "example.com",
            "--allowed-host-file",
            str(duplicate_path),
        ]
    )
    with pytest.raises(ValueError, match="unique"):
        _load_allowed_hosts(duplicate)

    oversized_path = (tmp_path / "oversized.txt").resolve()
    oversized_path.write_bytes(b"a" * (16 * 1024 + 1))
    oversized = build_parser().parse_args(
        [
            "--unix-socket",
            "/tmp/proxy.sock",
            "--allowed-host-file",
            str(oversized_path),
        ]
    )
    with pytest.raises(ValueError, match="bounded"):
        _load_allowed_hosts(oversized)

    linked_source = (tmp_path / "linked-source.txt").resolve()
    linked_source.write_text("example.net\n", encoding="utf-8")
    linked_policy = (tmp_path / "linked-policy.txt").resolve()
    os.link(linked_source, linked_policy)
    linked = build_parser().parse_args(
        [
            "--unix-socket",
            "/tmp/proxy.sock",
            "--allowed-host-file",
            str(linked_policy),
        ]
    )
    with pytest.raises(ValueError, match="bounded plain"):
        _load_allowed_hosts(linked)
