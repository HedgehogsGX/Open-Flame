from __future__ import annotations

import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path


class NetworkIsolationError(RuntimeError):
    pass


RelayProbe = Callable[[str, int, float], None]
PathStat = Callable[[Path], object]


class UnixRelayNetworkGuard:
    """Verify the downloader namespace has loopback only plus a safe UDS relay.

    This guard is intended for a Linux container started with
    ``network_mode: none``. The controlled proxy lives in another container and
    is reachable only through a shared Unix-domain socket; a small local relay
    exposes that socket on loopback for tools that only understand HTTP proxy
    URLs.
    """

    def __init__(
        self,
        *,
        unix_socket_path: Path,
        interface_root: Path = Path("/sys/class/net"),
        ipv4_routes: Path = Path("/proc/net/route"),
        ipv6_routes: Path = Path("/proc/net/ipv6_route"),
        relay_host: str = "127.0.0.1",
        relay_port: int = 18080,
        probe_timeout_seconds: float = 1.0,
        platform: str | None = None,
        relay_probe: RelayProbe | None = None,
        path_stat: PathStat | None = None,
    ) -> None:
        self.unix_socket_path = unix_socket_path
        self.interface_root = interface_root
        self.ipv4_routes = ipv4_routes
        self.ipv6_routes = ipv6_routes
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.probe_timeout_seconds = probe_timeout_seconds
        self.platform = platform or sys.platform
        self.relay_probe = relay_probe or self._probe_tcp
        self.path_stat = path_stat or (lambda path: path.stat())

    def assert_ready(self, *, adapter_name: str) -> None:
        del adapter_name
        if self.platform != "linux":
            raise NetworkIsolationError(
                "controlled downloader mode requires a Linux network namespace"
            )
        if self.relay_host not in {"127.0.0.1", "::1"}:
            raise NetworkIsolationError("local egress relay must bind to loopback")
        try:
            interfaces = {entry.name for entry in self.interface_root.iterdir()}
        except OSError as exc:
            raise NetworkIsolationError("cannot inspect network interfaces") from exc
        if interfaces != {"lo"}:
            raise NetworkIsolationError(
                "downloader namespace must contain only the loopback interface"
            )
        self._require_loopback_only_routes(self.ipv4_routes, ipv6=False)
        self._require_loopback_only_routes(self.ipv6_routes, ipv6=True)
        try:
            info = self.path_stat(self.unix_socket_path)
        except OSError as exc:
            raise NetworkIsolationError("controlled egress socket is unavailable") from exc
        mode = getattr(info, "st_mode", 0)
        if not stat.S_ISSOCK(mode):
            raise NetworkIsolationError("controlled egress path is not a Unix socket")
        if mode & stat.S_IWOTH:
            raise NetworkIsolationError("controlled egress socket is world-writable")
        try:
            self.relay_probe(
                self.relay_host,
                self.relay_port,
                self.probe_timeout_seconds,
            )
        except OSError as exc:
            raise NetworkIsolationError("local egress relay is unavailable") from exc

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout: float) -> None:
        with socket.create_connection((host, port), timeout=timeout):
            pass

    @staticmethod
    def _require_loopback_only_routes(path: Path, *, ipv6: bool) -> None:
        try:
            lines = path.read_text(encoding="ascii").splitlines()
        except OSError as exc:
            raise NetworkIsolationError("cannot inspect network routes") from exc
        for index, line in enumerate(lines):
            fields = line.split()
            if not fields:
                continue
            if not ipv6 and index == 0 and fields[0] == "Iface":
                continue
            interface = fields[-1] if ipv6 else fields[0]
            if interface != "lo":
                raise NetworkIsolationError(
                    "downloader namespace contains a non-loopback route"
                )
