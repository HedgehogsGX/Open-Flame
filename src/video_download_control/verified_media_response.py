"""Stream already-open, verified media handles without reopening paths."""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from secrets import token_hex
from typing import BinaryIO

from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import Receive, Scope, Send

from .managed_files import close_binary_on_error

VERIFIED_STREAM_CHUNK_BYTES = 64 * 1024


def close_binary_handle(handle: BinaryIO) -> None:
    with suppress(Exception):
        handle.close()


class VerifiedOpenFileResponse(FileResponse):
    """Serve a verified open handle without reopening its filesystem path."""

    def __init__(
        self,
        handle: BinaryIO,
        *,
        file_info: os.stat_result,
        filename: str | None,
        expected_sha256: str,
        media_type: str = "application/octet-stream",
        content_disposition_type: str = "attachment",
        verified_chunk_sha256: tuple[bytes, ...] | None = None,
        verification_chunk_size: int = VERIFIED_STREAM_CHUNK_BYTES,
    ) -> None:
        with close_binary_on_error(handle):
            self._verified_handle = handle
            self._size_bytes = int(file_info.st_size)
            self._verified_chunk_sha256 = verified_chunk_sha256
            self._verification_chunk_size = verification_chunk_size
            if verified_chunk_sha256 is not None:
                if verification_chunk_size < 1:
                    raise ValueError("verified chunk manifest is invalid")
                expected_chunks = (
                    self._size_bytes + verification_chunk_size - 1
                ) // verification_chunk_size
                if (
                    len(verified_chunk_sha256) != expected_chunks
                    or any(len(item) != 32 for item in verified_chunk_sha256)
                ):
                    raise ValueError("verified chunk manifest is invalid")
            super().__init__(
                "<verified-original>",
                media_type=media_type,
                filename=filename,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "ETag": f'"{expected_sha256}"',
                },
                stat_result=file_info,
                content_disposition_type=content_disposition_type,
            )

    def _read_verified_chunk(self, index: int) -> bytes:
        assert self._verified_chunk_sha256 is not None
        start = index * self._verification_chunk_size
        expected_size = min(
            self._verification_chunk_size,
            self._size_bytes - start,
        )
        self._verified_handle.seek(start)
        remaining = expected_size
        chunks: list[bytes] = []
        while remaining:
            chunk = self._verified_handle.read(remaining)
            if not chunk:
                raise RuntimeError("verified original handle ended unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).digest() != self._verified_chunk_sha256[index]:
            raise RuntimeError("verified original chunk changed before response")
        return payload

    async def _send_span(
        self,
        send: Send,
        start: int,
        end: int,
        *,
        more_after: bool,
    ) -> None:
        if self._verified_chunk_sha256 is not None:
            position = start
            if position == end:
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": more_after,
                    }
                )
                return
            first = start // self._verification_chunk_size
            last = (end - 1) // self._verification_chunk_size
            for index in range(first, last + 1):
                whole = await run_in_threadpool(self._read_verified_chunk, index)
                chunk_start = index * self._verification_chunk_size
                lower = max(start - chunk_start, 0)
                upper = min(end - chunk_start, len(whole))
                payload = whole[lower:upper]
                position += len(payload)
                await send(
                    {
                        "type": "http.response.body",
                        "body": payload,
                        "more_body": position < end or more_after,
                    }
                )
            return
        await run_in_threadpool(self._verified_handle.seek, start)
        position = start
        if position == end:
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": more_after,
                }
            )
            return
        while position < end:
            chunk = await run_in_threadpool(
                self._verified_handle.read,
                min(self.chunk_size, end - position),
            )
            if not chunk:
                raise RuntimeError("verified original handle ended unexpectedly")
            position += len(chunk)
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": position < end or more_after,
                }
            )

    async def _handle_simple(
        self,
        send: Send,
        send_header_only: bool,
        _send_pathsend: bool,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._send_span(
            send,
            0,
            self._size_bytes,
            more_after=False,
        )

    async def _handle_single_range(
        self,
        send: Send,
        start: int,
        end: int,
        file_size: int,
        send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        await self._send_span(send, start, end, more_after=False)

    async def _handle_multiple_ranges(
        self,
        send: Send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges,
            boundary,
            file_size,
            self.headers["content-type"],
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send(
            {"type": "http.response.start", "status": 206, "headers": headers.raw}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        for start, end in ranges:
            await send(
                {
                    "type": "http.response.body",
                    "body": header_generator(start, end),
                    "more_body": True,
                }
            )
            await self._send_span(send, start, end, more_after=True)
            await send(
                {"type": "http.response.body", "body": b"\r\n", "more_body": True}
            )
        await send(
            {
                "type": "http.response.body",
                "body": f"--{boundary}--".encode("latin-1"),
                "more_body": False,
            }
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close_binary_handle(self._verified_handle)


__all__ = [
    "VERIFIED_STREAM_CHUNK_BYTES",
    "VerifiedOpenFileResponse",
    "close_binary_handle",
]
