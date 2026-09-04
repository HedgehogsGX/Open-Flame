"""Opt-in short-link HTTPS transport owned by the standalone control process.

This adapter reuses the signed protocol and numeric TLS fetch implementation;
it neither listens on a port nor creates a persistent event-loop thread.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Sequence

from .security.egress import ResolvedTarget
from .short_link_transport import (
    MemoryReplayStore,
    NumericConnector,
    ShortLinkEgressService,
    ShortLinkTransportConfigurationError,
    ShortLinkTransportLimits,
    SignedShortLinkTransport,
)
from .short_links import ShortLinkResolutionError, ShortLinkResponse


class LocalDirectShortLinkTransport:
    """Direct network access requires the standalone application's acknowledgement."""

    def __init__(
        self,
        *,
        acknowledged: bool,
        max_concurrent_requests: int = 4,
        connector: NumericConnector | None = None,
        limits: ShortLinkTransportLimits | None = None,
    ) -> None:
        if acknowledged is not True:
            raise ShortLinkTransportConfigurationError(
                "local short-link resolution requires direct-network acknowledgement"
            )
        self._connector = connector
        self._limits = limits or ShortLinkTransportLimits()
        if (
            isinstance(max_concurrent_requests, bool)
            or not isinstance(max_concurrent_requests, int)
            or not 1 <= max_concurrent_requests <= 256
        ):
            raise ShortLinkTransportConfigurationError(
                "local short-link concurrency must be an integer from 1 to 256"
            )
        self._slots = threading.BoundedSemaphore(
            min(max_concurrent_requests, self._limits.max_connections)
        )
        self._state_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Refuse new requests; already admitted requests keep their bounded lifetime."""

        with self._state_lock:
            self._closed = True

    def request(
        self,
        target: ResolvedTarget,
        *,
        method: str,
        headers: Sequence[tuple[str, str]],
        max_header_bytes: int,
        max_body_bytes: int,
        timeout_seconds: float,
    ) -> ShortLinkResponse:
        acquired = False
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise ShortLinkResolutionError("transport_failure", "本地短链请求失败")
            with self._state_lock:
                if not self._closed:
                    acquired = self._slots.acquire(blocking=False)
            if not acquired:
                raise ShortLinkResolutionError("transport_failure", "本地短链请求失败")
            shared_key = secrets.token_bytes(32)

            def exchange(packet: bytes, timeout: float) -> bytes:
                async def run() -> bytes:
                    service = ShortLinkEgressService(
                        shared_key=shared_key,
                        replay_store=MemoryReplayStore(),
                        connector=self._connector,
                        limits=self._limits,
                    )
                    try:
                        async with asyncio.timeout(timeout):
                            return await service.handle_frame(packet)
                    finally:
                        await service.close()

                coroutine = run()
                try:
                    return asyncio.run(coroutine)
                except TimeoutError:
                    raise ShortLinkResolutionError(
                        "transport_exchange_timeout", "本地短链请求失败"
                    ) from None
                finally:
                    # asyncio.run may fail before it takes ownership of the coroutine.
                    coroutine.close()

            transport = SignedShortLinkTransport(
                shared_key=shared_key,
                exchange=exchange,
                limits=self._limits,
            )
            return transport.request(
                target,
                method=method,
                headers=headers,
                max_header_bytes=max_header_bytes,
                max_body_bytes=max_body_bytes,
                timeout_seconds=timeout_seconds,
            )
        except ShortLinkResolutionError as failure:
            reason = failure.reason
        except Exception:  # noqa: BLE001 - no upstream details cross this boundary
            reason = "transport_failure"
        finally:
            if acquired:
                self._slots.release()
        raise ShortLinkResolutionError(reason, "本地短链请求失败")
