from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_download_control.security import (
    NetworkIsolationError,
    UnixRelayNetworkGuard,
)


def guard_fixture(tmp_path: Path, *, interfaces: tuple[str, ...] = ("lo",)):
    interface_root = tmp_path / "net"
    interface_root.mkdir(parents=True)
    for interface in interfaces:
        (interface_root / interface).mkdir()
    ipv4 = tmp_path / "route"
    ipv4.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "lo 0000007F 00000000 0001 0 0 0 000000FF 0 0 0\n",
        encoding="ascii",
    )
    ipv6 = tmp_path / "ipv6_route"
    ipv6.write_text(
        "00000000000000000000000000000001 80 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000000 00000000 00000000 00000001 lo\n",
        encoding="ascii",
    )
    probes: list[tuple[str, int, float]] = []
    guard = UnixRelayNetworkGuard(
        unix_socket_path=tmp_path / "proxy.sock",
        interface_root=interface_root,
        ipv4_routes=ipv4,
        ipv6_routes=ipv6,
        platform="linux",
        relay_probe=lambda host, port, timeout: probes.append((host, port, timeout)),
        path_stat=lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660),
    )
    return guard, probes, ipv4


def test_guard_accepts_loopback_only_namespace_and_private_socket(
    tmp_path: Path,
) -> None:
    guard, probes, _ = guard_fixture(tmp_path)
    guard.assert_ready(adapter_name="yt-dlp")
    assert probes == [("127.0.0.1", 18080, 1.0)]


def test_guard_rejects_non_linux_or_any_non_loopback_interface(
    tmp_path: Path,
) -> None:
    guard, _, _ = guard_fixture(tmp_path)
    guard.platform = "win32"
    with pytest.raises(NetworkIsolationError, match="Linux"):
        guard.assert_ready(adapter_name="yt-dlp")

    unsafe, _, _ = guard_fixture(tmp_path / "unsafe", interfaces=("lo", "eth0"))
    with pytest.raises(NetworkIsolationError, match="loopback interface"):
        unsafe.assert_ready(adapter_name="yt-dlp")


def test_guard_rejects_non_loopback_route_and_world_writable_socket(
    tmp_path: Path,
) -> None:
    guard, _, ipv4 = guard_fixture(tmp_path)
    with ipv4.open("a", encoding="ascii") as handle:
        handle.write("eth0 00000000 00000000 0003 0 0 0 00000000 0 0 0\n")
    with pytest.raises(NetworkIsolationError, match="non-loopback route"):
        guard.assert_ready(adapter_name="yt-dlp")

    insecure, _, _ = guard_fixture(tmp_path / "insecure")
    insecure.path_stat = lambda path: SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o666
    )
    with pytest.raises(NetworkIsolationError, match="world-writable"):
        insecure.assert_ready(adapter_name="yt-dlp")


def test_guard_rejects_regular_file_or_failed_relay_probe(tmp_path: Path) -> None:
    guard, _, _ = guard_fixture(tmp_path)
    guard.path_stat = lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o600)
    with pytest.raises(NetworkIsolationError, match="not a Unix socket"):
        guard.assert_ready(adapter_name="yt-dlp")

    unavailable, _, _ = guard_fixture(tmp_path / "unavailable")

    def fail_probe(host: str, port: int, timeout: float) -> None:
        raise OSError("not listening")

    unavailable.relay_probe = fail_probe
    with pytest.raises(NetworkIsolationError, match="relay is unavailable"):
        unavailable.assert_ready(adapter_name="yt-dlp")
