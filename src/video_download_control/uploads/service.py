"""Durable, explicit upload submission, separate from download recovery.

An interrupted upload may already exist on the platform. Such an operation is
never retried automatically. The immutable local draft remains reviewable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import struct
import threading
import time
import zlib
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from ..managed_files import (
    ManagedFileChanged,
    ManagedFileSizeExceeded,
    UnsafeManagedPath,
    file_signature,
    hash_open_binary,
    lstat_plain,
    open_matching_binary,
    require_matching_fstat,
)

from .activity_lock import (
    UploadActivityBusy,
    UploadActivityLease,
    canonical_activity_root,
    upload_activity_lock,
)
from .contracts import (
    BackendResult,
    PLATFORMS,
    UploadBackend,
    UploadError,
    UploadRequest,
    is_tencent_short_title_output,
    normalize_tencent_short_title,
)
from .identity import normalize_account_bindings, normalize_upload_job_batch
from .login_progress import validate_update
from .metadata import (
    DOUYIN_DECLARATIONS,
    SCHEDULE_LEAD_SECONDS,
    TARGET_OVERRIDE_KEYS,
    TENCENT_CONTENT_LABELS,
    TENCENT_SCHEDULE_MAX_SECONDS,
    TITLE_LIMITS,
    normalize_platform_options,
    normalize_upload_tags,
    normalize_upload_text,
    validate_publish_schedule,
)
from .schema import UploadSchemaError, ensure_upload_schema

MAX_SOURCE_BYTES = 2 * 1024**3
MAX_COVER_BYTES = 20 * 1024**2
MAX_COVER_DECODED_BYTES = 64 * 1024**2
MAX_COVER_PIXELS = 40_000_000
UPLOAD_RESERVE_BYTES = 64 * 1024**2
_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_UPLOAD_AUTH_INVALID_CODES = frozenset({"account_invalid", "account_missing"})
_PAGE_CURSOR = re.compile(r"^([0-3]):([1-9][0-9]*)$")
_JOB_PRIORITY = "CASE WHEN j.state='running' THEN 0 WHEN j.state='queued' THEN 1 WHEN j.state IN ('draft','unknown','failed','canceled') THEN 2 ELSE 3 END"
_SOURCE_PRIORITY = ("CASE WHEN EXISTS(SELECT 1 FROM jobs active WHERE active.source_id=s.id "
                    "AND active.state='running') THEN 0 "
                    "WHEN EXISTS(SELECT 1 FROM jobs queued WHERE queued.source_id=s.id "
                    "AND queued.state='queued') THEN 1 "
                     "WHEN EXISTS(SELECT 1 FROM jobs actionable WHERE actionable.source_id=s.id "
                     "AND actionable.state IN ('draft','unknown','failed','canceled')) THEN 2 ELSE 3 END")
_ACTIVE_SOURCE_STATES = ("draft", "queued", "running")
_IMAGE_MODE_BYTES_PER_PIXEL = {
    "1": 1,
    "L": 1,
    "LA": 2,
    "P": 1,
    "PA": 2,
    "I": 4,
    "F": 4,
    "RGB": 3,
    "RGBX": 4,
    "RGBA": 4,
    "CMYK": 4,
    "YCbCr": 3,
    "LAB": 3,
    "HSV": 3,
    "I;16": 2,
    "I;16B": 2,
    "I;16L": 2,
    "I;16N": 2,
}


def default_upload_root(data_root: Path) -> Path:
    """Keep secrets outside the downloader's recursively copied backup root."""
    return data_root.with_name(data_root.name + "-uploads")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        return lstat_plain(path, directory=directory)
    except UnsafeManagedPath:
        raise UploadError("unsafe_upload_file") from None


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise UploadError("invalid_identifier")
    return value


def _png_dimensions(payload: bytes) -> tuple[int, int] | None:
    """Validate a complete, non-interlaced PNG image stream."""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset, dimensions, saw_data, saw_end = 8, None, False, False
    compressed: list[bytes] = []
    bit_depth = colour_type = None
    palette = False
    ended_data = False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        if length > MAX_COVER_BYTES or offset + 12 + length > len(payload):
            return None
        kind = payload[offset + 4:offset + 8]
        data = payload[offset + 8:offset + 8 + length]
        expected = struct.unpack(">I", payload[offset + 8 + length:offset + 12 + length])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected:
            return None
        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                return None
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                            4: {8, 16}, 6: {8, 16}}
            if (not 1 <= width <= 32768 or not 1 <= height <= 32768
                    or width * height > MAX_COVER_PIXELS
                    or depth not in valid_depths.get(colour, set())
                    or compression != 0 or filtering != 0 or interlace != 0):
                return None
            dimensions = (width, height)
            bit_depth, colour_type = depth, colour
        elif kind == b"acTL":
            return None
        elif kind == b"PLTE":
            if saw_data or not 3 <= length <= 768 or length % 3:
                return None
            palette = True
        elif kind == b"IDAT":
            if ended_data:
                return None
            saw_data = True
            compressed.append(data)
        elif kind == b"IEND":
            if length != 0 or offset + 12 != len(payload):
                return None
            saw_end = True
            break
        elif saw_data:
            ended_data = True
        offset += 12 + length
    if not dimensions or not saw_data or not saw_end or colour_type == 3 and not palette:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    row_bytes = (dimensions[0] * channels * bit_depth + 7) // 8
    expected_size = dimensions[1] * (row_bytes + 1)
    if expected_size > MAX_COVER_DECODED_BYTES:
        return None
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(b"".join(compressed), expected_size + 1)
    except zlib.error:
        return None
    # A max_length stop leaves compressed input in unconsumed_tail. Reject it
    # before flush: flush() has no output cap and would expand an image bomb in
    # memory before the size comparison below could run.
    if (not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail
            or len(decoded) != expected_size):
        return None
    stride = row_bytes + 1
    if any(decoded[offset] > 4 for offset in range(0, expected_size, stride)):
        return None
    return dimensions


def _jpeg_structure_dimensions(payload: bytes) -> tuple[int, int] | None:
    """Validate JPEG framing, tables and scans before decoder verification."""
    if len(payload) < 4 or payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
        return None
    offset, dimensions = 2, None
    frame_components: set[int] = set()
    saw_quantization = saw_coding = saw_scan = False
    minimum_entropy_bits = total_entropy_bytes = 0
    in_scan = False
    pending_marker: int | None = None
    while pending_marker is not None or offset < len(payload):
        if in_scan:
            entropy_bytes = 0
            while offset < len(payload):
                if payload[offset] != 0xFF:
                    entropy_bytes += 1
                    offset += 1
                    continue
                offset += 1
                while offset < len(payload) and payload[offset] == 0xFF:
                    offset += 1
                if offset >= len(payload):
                    return None
                marker = payload[offset]
                offset += 1
                if marker == 0x00:
                    entropy_bytes += 1
                    continue
                if 0xD0 <= marker <= 0xD7:
                    continue
                if entropy_bytes == 0:
                    return None
                total_entropy_bytes += entropy_bytes
                saw_scan = True
                in_scan = False
                pending_marker = marker
                break
            if in_scan:
                return None
            continue
        if pending_marker is None:
            if offset >= len(payload) or payload[offset] != 0xFF:
                return None
            while offset < len(payload) and payload[offset] == 0xFF:
                offset += 1
            if offset >= len(payload):
                return None
            marker = payload[offset]
            offset += 1
        else:
            marker = pending_marker
            pending_marker = None
        if marker == 0xD9:
            return (
                dimensions
                if offset == len(payload) and dimensions is not None and saw_scan
                and saw_quantization and saw_coding
                and total_entropy_bytes * 8 >= minimum_entropy_bits
                else None
            )
        if marker in {0x00, 0x01, 0xD8, *range(0xD0, 0xD8)}:
            return None
        if offset + 2 > len(payload):
            return None
        length = struct.unpack(">H", payload[offset:offset + 2])[0]
        if length < 2 or offset + length > len(payload):
            return None
        segment = payload[offset + 2:offset + length]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if dimensions is not None or len(segment) < 9:
                return None
            height, width = struct.unpack(">HH", segment[1:5])
            component_count = segment[5]
            if (not 1 <= width <= 32768 or not 1 <= height <= 32768
                    or width * height > MAX_COVER_PIXELS
                    or segment[0] not in {8, 12}
                    or not 1 <= component_count <= 4
                    or len(segment) != 6 + component_count * 3):
                return None
            components = [segment[index] for index in range(6, len(segment), 3)]
            if len(set(components)) != component_count:
                return None
            sampling: list[tuple[int, int]] = []
            for index in range(6, len(segment), 3):
                horizontal, vertical = segment[index + 1] >> 4, segment[index + 1] & 15
                if not 1 <= horizontal <= 4 or not 1 <= vertical <= 4 or segment[index + 2] > 3:
                    return None
                sampling.append((horizontal, vertical))
            dimensions = (width, height)
            frame_components = set(components)
            maximum_horizontal = max(value[0] for value in sampling)
            maximum_vertical = max(value[1] for value in sampling)
            mcu_columns = (width + 8 * maximum_horizontal - 1) // (8 * maximum_horizontal)
            mcu_rows = (height + 8 * maximum_vertical - 1) // (8 * maximum_vertical)
            minimum_entropy_bits = mcu_columns * mcu_rows * sum(
                horizontal * vertical for horizontal, vertical in sampling
            )
        elif marker == 0xDB:
            cursor = 0
            while cursor < len(segment):
                precision, table = segment[cursor] >> 4, segment[cursor] & 15
                table_bytes = 64 * (precision + 1)
                if precision > 1 or table > 3 or cursor + 1 + table_bytes > len(segment):
                    return None
                values = segment[cursor + 1:cursor + 1 + table_bytes]
                if (
                    not precision and 0 in values
                    or precision and any(
                        values[index:index + 2] == b"\x00\x00"
                        for index in range(0, len(values), 2)
                    )
                ):
                    return None
                cursor += 1 + table_bytes
            if not segment or cursor != len(segment):
                return None
            saw_quantization = True
        elif marker == 0xC4:
            cursor = 0
            while cursor < len(segment):
                table_class, table = segment[cursor] >> 4, segment[cursor] & 15
                if table_class > 1 or table > 3 or cursor + 17 > len(segment):
                    return None
                symbol_count = sum(segment[cursor + 1:cursor + 17])
                if symbol_count == 0 or symbol_count > 256 or cursor + 17 + symbol_count > len(segment):
                    return None
                available = 1
                for count in segment[cursor + 1:cursor + 17]:
                    available = available * 2 - count
                    if available < 0:
                        return None
                cursor += 17 + symbol_count
            if not segment or cursor != len(segment):
                return None
            saw_coding = True
        elif marker == 0xCC:
            if not segment or len(segment) % 2 or any(segment[index] > 0x1F for index in range(0, len(segment), 2)):
                return None
            saw_coding = True
        if marker == 0xDA:
            scan_components = segment[0] if segment else 0
            if (
                dimensions is None
                or not saw_quantization
                or not saw_coding
                or not 1 <= scan_components <= len(frame_components)
                or len(segment) != 4 + scan_components * 2
            ):
                return None
            component_ids = [segment[index] for index in range(1, 1 + scan_components * 2, 2)]
            if len(set(component_ids)) != scan_components or not set(component_ids) <= frame_components:
                return None
            for index in range(2, 1 + scan_components * 2, 2):
                if segment[index] >> 4 > 3 or segment[index] & 15 > 3:
                    return None
            spectral_start, spectral_end, approximation = segment[-3:]
            if spectral_start > spectral_end or spectral_end > 63 or approximation >> 4 > 13 or approximation & 15 > 13:
                return None
            in_scan = True
        offset += length
    return None


def _webp_structure_dimensions(payload: bytes) -> tuple[int, int] | None:
    if (len(payload) < 30 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP"
            or struct.unpack("<I", payload[4:8])[0] + 8 != len(payload)):
        return None
    offset, canvas, image = 12, None, None
    while offset + 8 <= len(payload):
        kind = payload[offset:offset + 4]
        chunk_size = struct.unpack("<I", payload[offset + 4:offset + 8])[0]
        end = offset + 8 + chunk_size
        padded_end = end + (chunk_size & 1)
        if padded_end > len(payload):
            return None
        data = payload[offset + 8:end]
        if kind in {b"ANIM", b"ANMF"}:
            return None
        if kind == b"VP8X":
            if canvas is not None or chunk_size != 10 or data[0] & 0x02:
                return None
            canvas = (1 + int.from_bytes(data[4:7], "little"),
                      1 + int.from_bytes(data[7:10], "little"))
        elif kind == b"VP8 ":
            if image is not None or chunk_size < 10 or data[3:6] != b"\x9d\x01\x2a":
                return None
            frame_tag = int.from_bytes(data[:3], "little")
            first_partition_size = frame_tag >> 5
            if frame_tag & 1 or not frame_tag & 0x10 or not first_partition_size or 10 + first_partition_size > chunk_size:
                return None
            image = (struct.unpack("<H", data[6:8])[0] & 0x3FFF,
                     struct.unpack("<H", data[8:10])[0] & 0x3FFF)
        elif kind == b"VP8L":
            if image is not None or chunk_size <= 5 or data[0] != 0x2F:
                return None
            bits = int.from_bytes(data[1:5], "little")
            if bits >> 29:
                return None
            image = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        offset = padded_end
    if offset != len(payload) or image is None or canvas is not None and canvas != image:
        return None
    width, height = image
    return ((width, height) if 1 <= width <= 32768 and 1 <= height <= 32768
            and width * height <= MAX_COVER_PIXELS else None)


def _decoded_image_dimensions(
    payload: bytes, expected_format: str,
) -> tuple[int, int] | None:
    """Fully decode one bounded, static image with the pinned Pillow codec."""
    try:
        with Image.open(BytesIO(payload)) as image:
            dimensions = image.size
            bytes_per_pixel = _IMAGE_MODE_BYTES_PER_PIXEL.get(image.mode)
            orientation = image.getexif().get(274, 1)
            if (
                image.format != expected_format
                or getattr(image, "n_frames", 1) != 1
                or type(orientation) is not int
                or orientation != 1
                or not 1 <= dimensions[0] <= 32768
                or not 1 <= dimensions[1] <= 32768
                or dimensions[0] * dimensions[1] > MAX_COVER_PIXELS
                or bytes_per_pixel is None
                or dimensions[0] * dimensions[1] * bytes_per_pixel
                > MAX_COVER_DECODED_BYTES
            ):
                return None
            image.load()
            loaded_bytes_per_pixel = _IMAGE_MODE_BYTES_PER_PIXEL.get(image.mode)
            if (
                image.size != dimensions
                or loaded_bytes_per_pixel is None
                or dimensions[0] * dimensions[1] * loaded_bytes_per_pixel
                > MAX_COVER_DECODED_BYTES
            ):
                return None
        return dimensions
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError):
        return None


def _cover_metadata(payload: bytes, suffix: str) -> tuple[str, int, int]:
    parsers = {
        ".png": ("image/png", "PNG", _png_dimensions),
        ".jpg": ("image/jpeg", "JPEG", _jpeg_structure_dimensions),
        ".jpeg": ("image/jpeg", "JPEG", _jpeg_structure_dimensions),
        ".webp": ("image/webp", "WEBP", _webp_structure_dimensions),
    }
    selected = parsers.get(suffix)
    structure = selected[2](payload) if selected is not None else None
    decoded = (
        _decoded_image_dimensions(payload, selected[1])
        if selected is not None and structure is not None
        else None
    )
    if selected is None or structure is None or decoded != structure:
        raise UploadError("invalid_cover_image")
    return selected[0], structure[0], structure[1]


def _requires_activity(method):
    """Keep a shared lease across a complete public mutation."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._activity():
            return method(self, *args, **kwargs)

    return locked


class _SchedulerLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        if self.path.exists():
            _plain(self.path)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class UploadService:
    def __init__(self, root: Path, backend: UploadBackend | None = None):
        requested_root = Path(os.path.abspath(root))
        # Every existing ancestor must be a plain directory, including junctions.
        for ancestor in reversed((requested_root, *requested_root.parents)):
            if ancestor.exists():
                _plain(ancestor, directory=True)
        requested_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root = canonical_activity_root(requested_root)
            with upload_activity_lock(self.root, exclusive=False):
                for ancestor in reversed((self.root.parent, *self.root.parent.parents)):
                    if ancestor.exists():
                        _plain(ancestor, directory=True)
                database_path = self.root / "uploads.sqlite3"
                if not database_path.exists():
                    self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    _plain(self.root, directory=True)
                else:
                    _plain(database_path)
                if canonical_activity_root(self.root) != self.root:
                    raise UploadActivityBusy("upload activity root changed during setup")
                ensure_upload_schema(database_path)
                for name in ("media", "assets", "incoming", "private"):
                    path = self.root / name
                    path.mkdir(exist_ok=True, mode=0o700)
                    _plain(path, directory=True)
        except UploadActivityBusy:
            raise UploadError("upload_activity_busy") from None
        except UploadSchemaError:
            raise UploadError("upload_schema_unsupported") from None
        self.database_path = database_path
        if backend is None:
            from .backend import SauBackend
            backend = SauBackend(self.root)
        self.backend = backend
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._operation_stop = threading.Event()
        self._active_id: str | None = None
        self._active_guard = threading.RLock()
        self._activity_lease_guard = threading.Lock()
        self._login_presentations: dict[str, dict] = {}
        self._source_integrity_cache: dict[str, tuple[tuple, bool]] = {}
        self._asset_integrity_cache: dict[str, tuple[tuple, bool]] = {}
        self._thread: threading.Thread | None = None
        self._activity_lease: UploadActivityLease | None = None
        self._lock = _SchedulerLock(self.root / ".worker.lock")
        self._scheduler_state = "stopped"
        self._scheduler_code = ""

    def _acquire_activity_lease(self) -> UploadActivityLease:
        try:
            return UploadActivityLease.acquire(self.root, exclusive=False)
        except UploadActivityBusy:
            raise UploadError("upload_activity_busy") from None

    @contextmanager
    def _activity(self):
        lease = self._acquire_activity_lease()
        try:
            yield
        finally:
            lease.release()

    @contextmanager
    def _managed_import(self, kind: str, managed_id: str | None):
        if managed_id is None:
            yield
            return
        managed_id = _identifier(managed_id)
        if kind not in {"source", "cover"}:
            raise UploadError("invalid_managed_import")
        directory = self.root / "private" / "import-locks"
        try:
            directory.mkdir(exist_ok=True, mode=0o700)
            _plain(directory, directory=True)
        except OSError:
            raise UploadError("managed_import_lock_unavailable") from None
        lock = _SchedulerLock(directory / f"{kind}-{managed_id}.lock")
        try:
            if not lock.acquire():
                raise UploadError("managed_import_in_progress")
            yield
        finally:
            lock.release()

    @staticmethod
    def _expected_sha256(value: str | None) -> str | None:
        if value is not None and (
            not isinstance(value, str) or not _SHA256.fullmatch(value)
        ):
            raise UploadError("invalid_expected_sha256")
        return value

    @staticmethod
    def _stable_digest(
        path: Path,
        before: os.stat_result,
        *,
        maximum: int,
        size_code: str,
        changed_code: str,
        unavailable_code: str,
    ) -> tuple[int, str]:
        expected = file_signature(before)
        try:
            opened = open_matching_binary(path, expected=expected)
            with opened.handle as handle:
                hashed = hash_open_binary(handle, maximum=maximum)
                require_matching_fstat(handle, expected=expected)
            if hashed.size != before.st_size or file_signature(_plain(path)) != expected:
                raise ManagedFileChanged
        except UploadError:
            raise
        except ManagedFileSizeExceeded:
            raise UploadError(size_code) from None
        except ManagedFileChanged:
            raise UploadError(changed_code) from None
        except OSError:
            raise UploadError(unavailable_code) from None
        return hashed.size, hashed.hexdigest

    @staticmethod
    def _stable_cover_payload(
        path: Path, before: os.stat_result,
    ) -> tuple[bytes, str]:
        expected = file_signature(before)
        try:
            opened = open_matching_binary(path, expected=expected)
            with opened.handle as handle:
                payload = handle.read(MAX_COVER_BYTES + 1)
                require_matching_fstat(handle, expected=expected)
            if len(payload) > MAX_COVER_BYTES:
                raise UploadError("cover_size_invalid")
            if len(payload) != before.st_size or file_signature(_plain(path)) != expected:
                raise UploadError("cover_changed")
        except UploadError:
            raise
        except ManagedFileChanged:
            raise UploadError("cover_changed") from None
        except OSError:
            raise UploadError("cover_unavailable") from None
        return payload, hashlib.sha256(payload).hexdigest()

    def _release_lifetime_activity(self) -> None:
        with self._activity_lease_guard:
            lease = self._activity_lease
            self._activity_lease = None
        if lease is not None:
            lease.release()

    @contextmanager
    def _db(self, *, timeout: float = 10):
        with self._activity():
            db = sqlite3.connect(self.database_path, timeout=timeout)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA journal_mode=WAL")
                with db:
                    yield db
            finally:
                db.close()

    def start(self) -> None:
        candidate = self._acquire_activity_lease()
        try:
            with self._active_guard:
                if self._thread is not None and self._thread.is_alive():
                    if self._scheduler_state != "faulted":
                        return
                    self._thread.join(timeout=1)
                    if self._thread.is_alive():
                        raise UploadError("upload_worker_stopping")
                with self._activity_lease_guard:
                    if self._activity_lease is None:
                        self._activity_lease = candidate
                        candidate = None
                try:
                    scheduler_owned = self._lock.acquire()
                except BaseException:
                    self._scheduler_state = "faulted"
                    self._scheduler_code = "scheduler_failed"
                    self._release_lifetime_activity()
                    raise
                if not scheduler_owned:
                    self._scheduler_state = "standby"
                    self._scheduler_code = "scheduler_owned_by_other_instance"
                    return  # Another application owns execution; read/queue still work.
                try:
                    was_faulted = self._scheduler_state == "faulted"
                    self._login_presentations.clear()
                    self._recover_interrupted_records()
                    self._scrub_disconnected_accounts()
                    self._shutdown.clear()
                    self._scheduler_state = "running"
                    self._scheduler_code = "scheduler_recovered" if was_faulted else ""
                    self._thread = threading.Thread(target=self._run, name="open-flame-uploads", daemon=True)
                    self._thread.start()
                except BaseException as exc:
                    self._scheduler_state = "faulted"
                    self._scheduler_code = ("scheduler_database_unavailable"
                                            if isinstance(exc, sqlite3.Error) else "scheduler_failed")
                    self._lock.release()
                    self._release_lifetime_activity()
                    raise
        finally:
            if candidate is not None:
                candidate.release()

    def stop(self) -> None:
        self._shutdown.set()
        with self._active_guard:
            self._operation_stop.set()
            self._login_presentations.clear()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=20)
            if thread.is_alive():
                raise UploadError("upload_worker_stopping")
        self._release_lifetime_activity()
        with self._active_guard:
            if self._scheduler_state != "faulted":
                self._scheduler_state = "stopped"
                self._scheduler_code = ""

    def status(self) -> dict:
        capabilities = {
            "bilibili": {
                "cover_slots": ["landscape"],
                "schedule_min_lead_seconds": SCHEDULE_LEAD_SECONDS["bilibili"],
                "fields": ["category_id", "copyright", "source_credit", "dynamic",
                           "no_reprint", "close_comments", "close_danmu"],
            },
            "douyin": {
                "cover_slots": ["landscape", "portrait"],
                "schedule_min_lead_seconds": SCHEDULE_LEAD_SECONDS["douyin"],
                "fields": ["declaration"],
                "declaration_values": sorted(DOUYIN_DECLARATIONS),
            },
            "tencent": {
                "cover_slots": ["landscape", "portrait"],
                "schedule_min_lead_seconds": SCHEDULE_LEAD_SECONDS["tencent"],
                "fields": ["short_title", "content_label"],
                "content_label_values": sorted(TENCENT_CONTENT_LABELS),
            },
        }
        return {"backend": self.backend.inspect(),
                "worker_running": bool(self._thread and self._thread.is_alive()
                                       and self._scheduler_state != "faulted"),
                "scheduler_state": self._scheduler_state,
                "scheduler_code": self._scheduler_code,
                "platforms": [{"id": p, "name": name, "title_limit": TITLE_LIMITS[p],
                               "description_limit": 2000, "tag_limit": 10,
                               "tag_length_limit": 20,
                               "modes": ["publish", "draft"] if p == "tencent" else ["publish"],
                               **capabilities[p]}
                              for p, name in PLATFORMS.items()],
                "max_source_bytes": MAX_SOURCE_BYTES,
                "max_cover_bytes": MAX_COVER_BYTES}

    def _require_workflow_runtime(self, *, execution_check: bool) -> None:
        """Verify the upload runtime and this instance's scheduler for workflows."""

        try:
            # Admission re-enumerates the tree with identity-bound digest
            # reuse. The created-to-download gate performs a fully uncached
            # check. Lightweight synthetic backends retain inspect().
            probe_name = (
                "inspect_for_execution" if execution_check else "inspect_for_workflow"
            )
            runtime_probe = getattr(self.backend, probe_name, None)
            backend = (
                runtime_probe()
                if callable(runtime_probe)
                else self.backend.inspect()
            )
        except Exception as error:
            code = str(error)
            if _SAFE_CODE.fullmatch(code) is None:
                code = "runtime_unavailable"
            raise UploadError(code) from None
        if not isinstance(backend, Mapping):
            raise UploadError("runtime_unavailable")
        if backend.get("ready") is not True:
            code = backend.get("code")
            if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
                code = "runtime_unavailable"
            raise UploadError(code)
        with self._active_guard:
            scheduler_state = self._scheduler_state
            scheduler_code = self._scheduler_code
            worker_running = bool(
                self._thread
                and self._thread.is_alive()
                and scheduler_state != "faulted"
            )
        if scheduler_state == "running" and worker_running:
            return
        if scheduler_state == "stopped":
            raise UploadError("uploader_stopped")
        if not isinstance(scheduler_code, str) or _SAFE_CODE.fullmatch(
            scheduler_code
        ) is None:
            scheduler_code = "upload_scheduler_not_ready"
        raise UploadError(scheduler_code or "upload_scheduler_not_ready")

    def accounts(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM accounts ORDER BY created_at,id")]

    def _remove_local_account_secret(self, platform: str, account_id: str) -> bool:
        remove = getattr(self.backend, "disconnect_local", None)
        if not callable(remove):
            return True
        try:
            remove(platform, account_id)
            return True
        except Exception:
            return False

    def _scrub_disconnected_accounts(self) -> None:
        with self._db() as db:
            rows = list(db.execute(
                "SELECT id,platform FROM accounts WHERE lifecycle_state='disconnected'"
            ))
        for row in rows:
            removed = self._remove_local_account_secret(row["platform"], row["id"])
            with self._db() as db:
                db.execute(
                    "UPDATE accounts SET code=? WHERE id=? AND lifecycle_state='disconnected'",
                    ("account_disconnected" if removed else "account_disconnect_cleanup_failed",
                     row["id"]),
                )

    @_requires_activity
    def disconnect_account(self, account_id: str) -> dict:
        """Disable a local session while keeping the account identity for history."""
        account_id = _identifier(account_id)
        with self._active_guard, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None:
                raise UploadError("account_not_found")
            if db.execute(
                "SELECT 1 FROM jobs WHERE account_id=? AND state='running' LIMIT 1",
                (account_id,),
            ).fetchone():
                raise UploadError("account_upload_active")
            now = _now()
            operation_ids = [row["id"] for row in db.execute(
                "SELECT id FROM operations WHERE account_id=? AND state IN ('queued','running')",
                (account_id,),
            )]
            revoked_confirmation_count = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE account_id=? AND state='queued'",
                (account_id,),
            ).fetchone()[0]
            db.execute(
                "UPDATE operations SET state='canceled',code='account_disconnected',updated_at=? "
                "WHERE account_id=? AND state IN ('queued','running')",
                (now, account_id),
            )
            db.execute(
                "UPDATE jobs SET state='draft',code='account_disconnected_confirmation_revoked',updated_at=? "
                "WHERE account_id=? AND state='queued'",
                (now, account_id),
            )
            db.execute(
                "UPDATE accounts SET auth_state='unchecked',code='account_disconnected',"
                "lifecycle_state='disconnected',disconnected_at=COALESCE(disconnected_at,?) "
                "WHERE id=?",
                (now, account_id),
            )
            if self._active_id in operation_ids:
                self._operation_stop.set()
            for operation_id in operation_ids:
                self._login_presentations.pop(operation_id, None)
            platform = account["platform"]
        removed = self._remove_local_account_secret(platform, account_id)
        with self._db() as db:
            db.execute(
                "UPDATE accounts SET code=? WHERE id=? AND lifecycle_state='disconnected'",
                ("account_disconnected" if removed else "account_disconnect_cleanup_failed",
                 account_id),
            )
            result = dict(db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
        if not removed:
            raise UploadError("account_disconnect_cleanup_failed")
        return {
            "account": result,
            "revoked_confirmation_count": revoked_confirmation_count,
            "canceled_operation_count": len(operation_ids),
            "local_login_removed": True,
        }

    @_requires_activity
    def add_account(self, platform: str, name: str) -> dict:
        if platform not in PLATFORMS:
            raise UploadError("unsupported_upload_platform")
        name = normalize_upload_text(name, 60, required=True)
        account_id = uuid4().hex
        try:
            with self._db() as db:
                db.execute("INSERT INTO accounts(id,platform,name,created_at) VALUES(?,?,?,?)", (account_id, platform, name, _now()))
                return dict(db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())
        except sqlite3.IntegrityError:
            raise UploadError("account_name_exists") from None

    @_requires_activity
    def account_action(self, account_id: str, action: str) -> dict:
        _identifier(account_id)
        if action not in ("login", "check"):
            raise UploadError("invalid_account_action")
        if (action == "login" and callable(getattr(self.backend, "login_interactive", None))
                and not (self._thread and self._thread.is_alive() and self._lock.handle)):
            raise UploadError("login_owned_by_other_instance")
        if not self.backend.inspect().get("ready"):
            raise UploadError("runtime_missing")
        operation_id, now = uuid4().hex, _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None:
                raise UploadError("account_not_found")
            if account["lifecycle_state"] != "active":
                raise UploadError("account_disconnected")
            if db.execute("SELECT id FROM operations WHERE account_id=? AND state IN ('queued','running')", (account_id,)).fetchone():
                raise UploadError("account_operation_active")
            db.execute("INSERT INTO operations(id,account_id,action,created_at,updated_at) VALUES(?,?,?,?,?)", (operation_id, account_id, action, now, now))
            db.execute("UPDATE accounts SET auth_state='checking',code='' WHERE id=?", (account_id,))
            if action == "login":
                # A new login may select a different real platform account.
                # Previous queued approvals must not follow that new session.
                db.execute("UPDATE jobs SET state='draft',code='account_session_changed',updated_at=? WHERE account_id=? AND state='queued'", (now, account_id))
            result = dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
        self._wake.set()
        return result

    def operations(self) -> list[dict]:
        with self._db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY created_at DESC,rowid DESC LIMIT 100")]
        with self._active_guard:
            for row in rows:
                view = self._login_view(row)
                row.update(login_phase=view.get("phase", row["state"]),
                           qr_available=bool(view.get("png")), qr_revision=view.get("revision", 0),
                           expires_at=view.get("expires_at"))
        return rows

    def _login_view(self, row: dict) -> dict:
        view = self._login_presentations.get(row["id"], {})
        if (row["action"] != "login" or row["state"] != "running"
                or row["code"] == "cancellation_requested" or self._active_id != row["id"]
                or self._operation_stop.is_set() or self._shutdown.is_set()):
            self._login_presentations.pop(row["id"], None)
            return {}
        if view.get("deadline") is not None and view["deadline"] <= time.monotonic():
            view.update(phase="expired", png=None)
        return view

    def login_qr(self, operation_id: str) -> bytes:
        _identifier(operation_id)
        with self._active_guard, self._db() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise UploadError("operation_not_found")
            view = self._login_view(dict(row))
            if not view.get("png") or view.get("phase") != "waiting_scan":
                raise UploadError("login_qr_unavailable")
            return view["png"]

    def _record_login(self, row: dict, stop: threading.Event, phase, png=None, expires_at=None) -> None:
        phase, png, expires_at = validate_update(phase, png, expires_at)
        with self._active_guard:
            if (self._active_id != row["id"] or self._operation_stop is not stop
                    or stop.is_set() or self._shutdown.is_set()):
                return
            with self._db() as db:
                current = db.execute("SELECT * FROM operations WHERE id=?", (row["id"],)).fetchone()
            if (current is None or current["state"] != "running"
                    or current["code"] == "cancellation_requested"
                    or current["account_id"] != row["account_id"] or current["action"] != "login"):
                return
            previous = self._login_presentations.get(row["id"], {})
            if (previous.get("phase") == phase and previous.get("png") == png
                    and previous.get("expires_at") == expires_at):
                return
            operation_deadline = previous.get("operation_deadline", time.monotonic() + 600)
            deadline = min(operation_deadline, time.monotonic() + max(0, expires_at - time.time())) if expires_at is not None else min(operation_deadline, time.monotonic() + 300)
            image_hash = hashlib.sha256(png).hexdigest() if png else None
            seen = dict(previous.get("seen", {}))
            if image_hash:
                if image_hash in seen:
                    old_deadline, old_expiry = seen[image_hash]
                    deadline, expires_at = min(old_deadline, deadline), old_expiry
                elif len(seen) >= 16:
                    raise ValueError("too_many_login_updates")
                seen[image_hash] = (deadline, expires_at)
            self._login_presentations[row["id"]] = {"phase": phase, "png": png,
                "expires_at": expires_at, "deadline": deadline, "image_hash": image_hash,
                "operation_deadline": operation_deadline, "seen": seen,
                "revision": previous.get("revision", 0) + 1}

    @_requires_activity
    def cancel_operation(self, operation_id: str) -> dict:
        _identifier(operation_id)
        with self._active_guard, self._db() as db:
            self._login_presentations.pop(operation_id, None)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise UploadError("operation_not_found")
            if row["state"] == "queued":
                db.execute("UPDATE operations SET state='canceled',code='canceled',updated_at=? WHERE id=?", (_now(), operation_id))
                db.execute("UPDATE accounts SET auth_state='unchecked',code='canceled' WHERE id=?", (row["account_id"],))
            elif row["state"] == "running":
                db.execute("UPDATE operations SET code='cancellation_requested' WHERE id=?", (operation_id,))
                if self._active_id == operation_id:
                    self._operation_stop.set()
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    @staticmethod
    def _page_key(cursor: str | None, limit: int) -> tuple[int, int] | None:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise UploadError("invalid_page_limit")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not (match := _PAGE_CURSOR.fullmatch(cursor)):
            raise UploadError("invalid_page_cursor")
        rowid = int(match.group(2))
        if rowid > 9_223_372_036_854_775_807:
            raise UploadError("invalid_page_cursor")
        return int(match.group(1)), rowid

    @staticmethod
    def _active_reference_sql() -> str:
        return ("(SELECT COUNT(*) FROM jobs active WHERE active.source_id=s.id "
                "AND active.state IN ('draft','queued','running'))")

    def _source_query(self) -> str:
        return "SELECT s.*," + self._active_reference_sql() + " AS active_reference_count FROM sources s"

    def source_page(self, *, cursor: str | None = None, limit: int = 50) -> dict:
        key = self._page_key(cursor, limit)
        query = ("SELECT * FROM (SELECT s.*,s.rowid AS _page_rowid,"
                 + self._active_reference_sql() + " AS active_reference_count,"
                 + _SOURCE_PRIORITY
                 + " AS _page_bucket FROM sources s) page")
        parameters: list[int] = []
        if key is not None:
            query += " WHERE (_page_bucket>? OR (_page_bucket=? AND _page_rowid<?))"
            parameters.extend((key[0], key[0], key[1]))
        query += " ORDER BY _page_bucket,_page_rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._db() as db:
            rows = list(db.execute(query, parameters))
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = f"{selected[-1]['_page_bucket']}:{selected[-1]['_page_rowid']}"
        return {"items": [self._source_public(row) for row in selected],
                "next_cursor": next_cursor}

    def sources(self) -> list[dict]:
        return self.source_page(limit=200)["items"]

    def source(self, source_id: str) -> dict:
        with self._db() as db:
            row = db.execute(self._source_query() + " WHERE s.id=?",
                             (_identifier(source_id),)).fetchone()
        if row is None:
            raise UploadError("source_not_found")
        return self._source_public(row)

    def _source_media_path(self, row) -> Path:
        return self.root / "media" / f"{row['id']}{row['suffix']}"

    def _source_media_state(self, row) -> tuple[str, int]:
        path = self._source_media_path(row)
        try:
            info = _plain(path)
        except FileNotFoundError:
            stored = row["media_state"] if "media_state" in row.keys() else "present"
            return ("deleted" if stored == "deleted" else "missing"), 0
        except (OSError, UploadError):
            return "unsafe", 0
        stored = row["media_state"] if "media_state" in row.keys() else "present"
        if stored != "present":
            return "changed", info.st_size
        if info.st_size != row["size"]:
            return "changed", info.st_size
        cached = self._source_integrity_cache.get(row["id"])
        if cached is not None and cached[0] == file_signature(info) and not cached[1]:
            return "changed", info.st_size
        return "present", info.st_size

    def _verified_source_media_path(self, row) -> Path:
        """Return a source path only after checking its recorded bytes."""
        if "media_state" in row.keys() and row["media_state"] != "present":
            raise UploadError("source_reimport_required")
        if row["suffix"] not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise UploadError("source_changed")
        _plain(self.root / "media", directory=True)
        path = self._source_media_path(row)
        try:
            before = _plain(path)
            if before.st_size != row["size"]:
                raise UploadError("source_changed")
            expected = file_signature(before)
            opened = open_matching_binary(path, expected=expected)
            with opened.handle as handle:
                hashed = hash_open_binary(handle, maximum=MAX_SOURCE_BYTES)
                require_matching_fstat(handle, expected=expected)
            after = _plain(path)
            valid = (
                hashed.size == before.st_size
                and hashed.hexdigest == row["sha256"]
                and file_signature(after) == expected
            )
            self._source_integrity_cache[row["id"]] = (file_signature(after), valid)
            if not valid:
                raise UploadError("source_changed")
        except (ManagedFileChanged, ManagedFileSizeExceeded):
            raise UploadError("source_changed") from None
        except OSError:
            raise UploadError("source_unavailable") from None
        return path

    def _source_public(self, row) -> dict:
        state, _actual_size = self._source_media_state(row)
        active = int(row["active_reference_count"]) if "active_reference_count" in row.keys() else 0
        result = {key: row[key] for key in ("id", "name", "size", "sha256", "created_at")}
        result.update(media_present=state == "present", media_state=state,
                      media_deleted_at=(row["deleted_at"] if "deleted_at" in row.keys() else None),
                      active_reference_count=active,
                      can_delete=state == "present" and active == 0)
        return result

    @staticmethod
    def _asset_active_reference_sql() -> str:
        return ("(SELECT COUNT(*) FROM jobs active WHERE "
                "(active.cover_landscape_asset_id=u.id OR "
                "active.cover_portrait_asset_id=u.id) "
                "AND active.state IN ('draft','queued','running'))")

    def _asset_query(self) -> str:
        return "SELECT u.*," + self._asset_active_reference_sql() + (
            " AS active_reference_count FROM upload_assets u"
        )

    def _asset_media_path(self, row) -> Path:
        return self.root / "assets" / f"{row['id']}{row['suffix']}"

    def _asset_media_state(self, row) -> tuple[str, int]:
        path = self._asset_media_path(row)
        try:
            info = _plain(path)
        except FileNotFoundError:
            return ("deleted" if row["media_state"] == "deleted" else "missing"), 0
        except (OSError, UploadError):
            return "unsafe", 0
        if row["media_state"] != "present":
            return "changed", info.st_size
        if info.st_size != row["size"]:
            return "changed", info.st_size
        cached = self._asset_integrity_cache.get(row["id"])
        if cached is not None and cached[0] == file_signature(info) and not cached[1]:
            return "changed", info.st_size
        return "present", info.st_size

    def _verified_asset_media(self, row) -> tuple[Path, bytes]:
        if row["kind"] != "cover" or row["media_state"] != "present":
            raise UploadError("cover_reimport_required")
        if row["suffix"] not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise UploadError("cover_changed")
        _plain(self.root / "assets", directory=True)
        path = self._asset_media_path(row)
        try:
            before = _plain(path)
            if before.st_size != row["size"]:
                raise UploadError("cover_changed")
            expected = file_signature(before)
            opened = open_matching_binary(path, expected=expected)
            with opened.handle as handle:
                payload = handle.read(MAX_COVER_BYTES + 1)
                require_matching_fstat(handle, expected=expected)
            try:
                mime_type, width, height = _cover_metadata(payload, row["suffix"])
            except UploadError:
                raise UploadError("cover_changed") from None
            digest = hashlib.sha256(payload).hexdigest()
            after = _plain(path)
            valid = (
                digest == row["sha256"]
                and len(payload) == row["size"]
                and mime_type == row["mime_type"]
                and width == row["width"]
                and height == row["height"]
                and file_signature(after) == expected
            )
            self._asset_integrity_cache[row["id"]] = (file_signature(after), valid)
            if not valid:
                raise UploadError("cover_changed")
        except FileNotFoundError:
            raise UploadError("cover_reimport_required") from None
        except ManagedFileChanged:
            raise UploadError("cover_changed") from None
        except OSError:
            raise UploadError("cover_unavailable") from None
        return path, payload

    def _verified_asset_media_path(self, row) -> Path:
        return self._verified_asset_media(row)[0]

    def _stage_cover_payload(self, job_id: str, slot: str, row, payload: bytes) -> Path:
        incoming = self.root / "incoming"
        _plain(incoming, directory=True)
        path = incoming / f"{job_id}-{slot}-{uuid4().hex}{row['suffix']}"
        try:
            with path.open("xb") as handle:
                if os.name != "nt":
                    os.chmod(path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            info = _plain(path)
            if info.st_size != len(payload):
                raise UploadError("cover_unavailable")
            return path
        except UploadError:
            path.unlink(missing_ok=True)
            raise
        except OSError:
            path.unlink(missing_ok=True)
            raise UploadError("cover_unavailable") from None

    def _asset_public(self, row) -> dict:
        state, _actual_size = self._asset_media_state(row)
        active = int(row["active_reference_count"]) if "active_reference_count" in row.keys() else 0
        result = {key: row[key] for key in (
            "id", "name", "size", "sha256", "mime_type", "width", "height", "created_at",
        )}
        result.update(media_present=state == "present", media_state=state,
                      media_deleted_at=row["deleted_at"], active_reference_count=active,
                      can_delete=state == "present" and active == 0)
        return result

    def cover_page(self, *, cursor: str | None = None, limit: int = 50) -> dict:
        key = self._page_key(cursor, limit)
        if key is not None and key[0] != 0:
            raise UploadError("invalid_page_cursor")
        query = (
            "SELECT * FROM (SELECT u.*,u.rowid AS _page_rowid,0 AS _page_bucket,"
            + self._asset_active_reference_sql()
            + " AS active_reference_count FROM upload_assets u WHERE u.kind='cover') page"
        )
        parameters: list[int] = []
        if key is not None:
            query += " WHERE _page_rowid<?"
            parameters.append(key[1])
        query += " ORDER BY _page_rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._db() as db:
            rows = list(db.execute(query, parameters))
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = f"0:{selected[-1]['_page_rowid']}"
        return {
            "items": [self._asset_public(row) for row in selected],
            "next_cursor": next_cursor,
        }

    def covers(self) -> list[dict]:
        return self.cover_page(limit=200)["items"]

    def covers_by_ids(self, asset_ids: list[str]) -> list[dict]:
        if (not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= 64
                or len(set(asset_ids)) != len(asset_ids)):
            raise UploadError("invalid_cover_ids")
        for asset_id in asset_ids:
            _identifier(asset_id)
        placeholders = ",".join("?" for _ in asset_ids)
        with self._db() as db:
            rows = db.execute(
                self._asset_query()
                + f" WHERE u.kind='cover' AND u.id IN ({placeholders})",
                asset_ids,
            )
            records = {row["id"]: self._asset_public(row) for row in rows}
        return [records[asset_id] for asset_id in asset_ids if asset_id in records]

    def cover(self, asset_id: str) -> dict:
        with self._db() as db:
            row = db.execute(self._asset_query() + " WHERE u.id=? AND u.kind='cover'",
                             (_identifier(asset_id),)).fetchone()
        if row is None:
            raise UploadError("cover_not_found")
        return self._asset_public(row)

    def cover_content(self, asset_id: str) -> tuple[bytes, str]:
        with self._db() as db:
            row = db.execute(self._asset_query() + " WHERE u.id=? AND u.kind='cover'",
                             (_identifier(asset_id),)).fetchone()
        if row is None:
            raise UploadError("cover_not_found")
        _path, payload = self._verified_asset_media(row)
        return payload, row["mime_type"]

    @_requires_activity
    def import_cover(
        self,
        path: Path,
        name: str,
        expected_sha256: str | None = None,
        managed_id: str | None = None,
    ) -> dict:
        name = normalize_upload_text(name, 180, required=True)
        if any(char in name for char in '/\\:\x00') or name in (".", ".."):
            raise UploadError("invalid_cover_name")
        suffix = Path(name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise UploadError("unsupported_cover_type")
        expected_sha256 = self._expected_sha256(expected_sha256)
        if managed_id is not None and expected_sha256 is None:
            raise UploadError("invalid_expected_sha256")
        asset_id = _identifier(managed_id) if managed_id is not None else uuid4().hex

        with self._managed_import("cover", managed_id):
            try:
                before = _plain(path)
            except OSError:
                raise UploadError("cover_unavailable") from None
            if not 0 < before.st_size <= MAX_COVER_BYTES:
                raise UploadError("cover_size_invalid")
            payload, digest = self._stable_cover_payload(path, before)
            if expected_sha256 is not None and digest != expected_sha256:
                raise UploadError("cover_hash_mismatch")
            mime_type, width, height = _cover_metadata(payload, suffix)

            with self._db() as db:
                row = db.execute(
                    self._asset_query() + " WHERE u.id=?", (asset_id,)
                ).fetchone()
            if row is not None:
                if managed_id is None:
                    raise UploadError("cover_import_conflict")
                if (
                    row["kind"] != "cover"
                    or row["name"] != name
                    or row["suffix"] != suffix
                    or row["mime_type"] != mime_type
                    or row["size"] != len(payload)
                    or row["sha256"] != digest
                    or row["width"] != width
                    or row["height"] != height
                    or row["media_state"] != "present"
                ):
                    raise UploadError("cover_import_conflict")
                try:
                    self._verified_asset_media_path(row)
                except UploadError:
                    raise UploadError("cover_import_conflict") from None
                return self._asset_public(row)

            assets_root = self.root / "assets"
            _plain(assets_root, directory=True)
            target = assets_root / f"{asset_id}{suffix}"
            if target.exists():
                if managed_id is None:
                    raise UploadError("cover_import_conflict")
                try:
                    target_before = _plain(target)
                    target_payload, target_digest = self._stable_cover_payload(
                        target, target_before
                    )
                    target_metadata = _cover_metadata(target_payload, suffix)
                except (OSError, UploadError):
                    raise UploadError("cover_import_conflict") from None
                if (
                    len(target_payload) != len(payload)
                    or target_digest != digest
                    or target_metadata != (mime_type, width, height)
                ):
                    raise UploadError("cover_import_conflict")
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "INSERT INTO upload_assets("
                        "id,kind,name,suffix,mime_type,size,sha256,width,height,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            asset_id,
                            "cover",
                            name,
                            suffix,
                            mime_type,
                            len(payload),
                            digest,
                            width,
                            height,
                            _now(),
                        ),
                    )
                    row = db.execute(
                        self._asset_query() + " WHERE u.id=?", (asset_id,)
                    ).fetchone()
                return self._asset_public(row)

            if shutil.disk_usage(self.root).free < len(payload) + UPLOAD_RESERVE_BYTES:
                raise UploadError("upload_storage_full")
            stage = assets_root / f".{asset_id}.{uuid4().hex}.import"
            published: Path | None = None
            try:
                with stage.open("xb") as handle:
                    if os.name != "nt":
                        os.chmod(stage, 0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if _plain(stage).st_size != len(payload):
                    raise UploadError("cover_unavailable")
                try:
                    os.link(stage, target)
                except FileExistsError:
                    raise UploadError("cover_import_conflict") from None
                except OSError:
                    raise UploadError("cover_import_failed") from None
                published = target
                stage.unlink()
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "INSERT INTO upload_assets("
                        "id,kind,name,suffix,mime_type,size,sha256,width,height,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            asset_id,
                            "cover",
                            name,
                            suffix,
                            mime_type,
                            len(payload),
                            digest,
                            width,
                            height,
                            _now(),
                        ),
                    )
                    row = db.execute(
                        self._asset_query() + " WHERE u.id=?", (asset_id,)
                    ).fetchone()
                published = None
                return self._asset_public(row)
            finally:
                stage.unlink(missing_ok=True)
                if published is not None:
                    published.unlink(missing_ok=True)

    @_requires_activity
    def delete_cover_media(self, asset_id: str) -> dict:
        asset_id = _identifier(asset_id)
        quarantine: Path | None = None
        target: Path | None = None
        with self._active_guard:
            try:
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(self._asset_query() + " WHERE u.id=?", (asset_id,)).fetchone()
                    if row is None:
                        raise UploadError("cover_not_found")
                    if row["active_reference_count"]:
                        raise UploadError("cover_in_use")
                    state, _actual_size = self._asset_media_state(row)
                    if state not in {"missing", "deleted"}:
                        if state != "present":
                            raise UploadError("cover_changed" if state == "changed" else "unsafe_upload_file")
                        target = self._verified_asset_media_path(row)
                        quarantine = target.with_name(f".{asset_id}.{uuid4().hex}.delete")
                        target.rename(quarantine)
                        db.execute(
                            "UPDATE upload_assets SET media_state='deleted',deleted_at=? WHERE id=?",
                            (_now(), asset_id),
                        )
                    updated = db.execute(self._asset_query() + " WHERE u.id=?", (asset_id,)).fetchone()
                    result = self._asset_public(updated)
            except BaseException as exc:
                if quarantine is not None and quarantine.exists() and target is not None:
                    try:
                        os.link(quarantine, target)
                        quarantine.unlink()
                    except OSError:
                        raise UploadError("cover_delete_rollback_failed") from exc
                raise
            if quarantine is not None:
                try:
                    quarantine.unlink()
                except OSError:
                    raise UploadError("cover_delete_cleanup_failed") from None
        return result

    def storage_usage(self) -> dict:
        with self._db() as db:
            rows = list(db.execute(self._source_query()))
            asset_rows = list(db.execute(self._asset_query()))
        registered_names = {self._source_media_path(row).name for row in rows}
        present = missing = deleted = changed = unsafe = managed_bytes = 0
        for row in rows:
            state, actual_size = self._source_media_state(row)
            if state == "missing":
                missing += 1
            elif state == "deleted":
                deleted += 1
            elif state == "unsafe":
                unsafe += 1
            elif state == "present":
                present += 1
                managed_bytes += actual_size
            elif state == "changed":
                changed += 1
                managed_bytes += actual_size
        asset_present = asset_missing = asset_deleted = asset_changed = asset_unsafe = 0
        for row in asset_rows:
            state, actual_size = self._asset_media_state(row)
            if state == "missing":
                asset_missing += 1
            elif state == "deleted":
                asset_deleted += 1
            elif state == "unsafe":
                asset_unsafe += 1
            elif state == "present":
                asset_present += 1
                managed_bytes += actual_size
            elif state == "changed":
                asset_changed += 1
                managed_bytes += actual_size
        orphan_count = orphan_bytes = unsafe_entries = 0
        media_root = self.root / "media"
        _plain(media_root, directory=True)
        try:
            entries = list(media_root.iterdir())
        except OSError:
            raise UploadError("source_storage_invalid") from None
        for entry in entries:
            if entry.name in registered_names:
                continue
            try:
                info = _plain(entry)
            except (OSError, UploadError):
                unsafe_entries += 1
            else:
                orphan_count += 1
                orphan_bytes += info.st_size
        registered_assets = {self._asset_media_path(row).name for row in asset_rows}
        assets_root = self.root / "assets"
        _plain(assets_root, directory=True)
        try:
            asset_entries = list(assets_root.iterdir())
        except OSError:
            raise UploadError("cover_storage_invalid") from None
        for entry in asset_entries:
            if entry.name in registered_assets:
                continue
            try:
                info = _plain(entry)
            except (OSError, UploadError):
                unsafe_entries += 1
            else:
                orphan_count += 1
                orphan_bytes += info.st_size
        disk = shutil.disk_usage(self.root)
        return {
            "registered_source_count": len(rows),
            "present_source_count": present,
            "missing_source_count": missing,
            "deleted_source_count": deleted,
            "changed_source_count": changed,
            "unsafe_source_count": unsafe,
            "registered_cover_count": len(asset_rows),
            "present_cover_count": asset_present,
            "missing_cover_count": asset_missing,
            "deleted_cover_count": asset_deleted,
            "changed_cover_count": asset_changed,
            "unsafe_cover_count": asset_unsafe,
            "managed_bytes": managed_bytes,
            "orphan_file_count": orphan_count,
            "orphan_bytes": orphan_bytes,
            "unsafe_entry_count": unsafe_entries,
            "free_bytes": disk.free,
            "reserve_bytes": UPLOAD_RESERVE_BYTES,
            "low_space": disk.free < UPLOAD_RESERVE_BYTES,
        }

    @_requires_activity
    def delete_source_media(self, source_id: str) -> dict:
        source_id = _identifier(source_id)
        quarantine: Path | None = None
        target: Path | None = None
        result: dict | None = None
        with self._active_guard:
            try:
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        self._source_query() + " WHERE s.id=?", (source_id,)
                    ).fetchone()
                    if row is None:
                        raise UploadError("source_not_found")
                    if row["active_reference_count"]:
                        raise UploadError("source_in_use")
                    state, _actual_size = self._source_media_state(row)
                    if state in {"missing", "deleted"}:
                        result = self._source_public(row)
                    else:
                        if state != "present":
                            raise UploadError(
                                "source_changed" if state == "changed" else "unsafe_upload_file"
                            )
                        target = self._source_path(source_id)
                        before = _plain(target)
                        quarantine = target.with_name(
                            f".{source_id}.{uuid4().hex}.delete"
                        )
                        try:
                            if file_signature(_plain(target)) != file_signature(before):
                                raise UploadError("source_changed")
                            target.rename(quarantine)
                        except FileNotFoundError:
                            raise UploadError("source_changed") from None
                        except OSError:
                            raise UploadError("source_delete_failed") from None
                        db.execute(
                            "UPDATE sources SET media_state='deleted',deleted_at=? WHERE id=?",
                            (_now(), source_id),
                        )
                        updated = db.execute(
                            self._source_query() + " WHERE s.id=?", (source_id,)
                        ).fetchone()
                        result = self._source_public(updated)
            except BaseException as exc:
                if quarantine is not None and quarantine.exists() and target is not None:
                    try:
                        os.link(quarantine, target)
                        quarantine.unlink()
                    except OSError:
                        raise UploadError("source_delete_rollback_failed") from exc
                raise
            if quarantine is not None:
                try:
                    quarantine.unlink()
                except OSError:
                    raise UploadError("source_delete_cleanup_failed") from None
        assert result is not None
        return result

    @_requires_activity
    def import_source(
        self,
        path: Path,
        name: str,
        expected_sha256: str | None = None,
        managed_id: str | None = None,
    ) -> dict:
        name = normalize_upload_text(name, 180, required=True)
        if any(char in name for char in '/\\:\x00') or name in (".", ".."):
            raise UploadError("invalid_source_name")
        suffix = Path(name).suffix.lower()
        if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise UploadError("unsupported_video_type")
        expected_sha256 = self._expected_sha256(expected_sha256)
        if managed_id is not None and expected_sha256 is None:
            raise UploadError("invalid_expected_sha256")
        source_id = _identifier(managed_id) if managed_id is not None else uuid4().hex

        with self._managed_import("source", managed_id):
            try:
                before = _plain(path)
            except OSError:
                raise UploadError("source_unavailable") from None
            if not 0 < before.st_size <= MAX_SOURCE_BYTES:
                raise UploadError("source_size_invalid")

            with self._db() as db:
                row = db.execute(
                    self._source_query() + " WHERE s.id=?", (source_id,)
                ).fetchone()
            if row is not None:
                if managed_id is None:
                    raise UploadError("source_import_conflict")
                total, digest = self._stable_digest(
                    path,
                    before,
                    maximum=MAX_SOURCE_BYTES,
                    size_code="source_size_invalid",
                    changed_code="source_changed",
                    unavailable_code="source_unavailable",
                )
                if expected_sha256 is not None and digest != expected_sha256:
                    raise UploadError("source_hash_mismatch")
                if (
                    row["name"] != name
                    or row["suffix"] != suffix
                    or row["size"] != total
                    or row["sha256"] != digest
                    or row["media_state"] != "present"
                ):
                    raise UploadError("source_import_conflict")
                try:
                    self._verified_source_media_path(row)
                except UploadError:
                    raise UploadError("source_import_conflict") from None
                return self._source_public(row)

            media_root = self.root / "media"
            _plain(media_root, directory=True)
            target = media_root / f"{source_id}{suffix}"
            if target.exists():
                if managed_id is None:
                    raise UploadError("source_import_conflict")
                total, digest = self._stable_digest(
                    path,
                    before,
                    maximum=MAX_SOURCE_BYTES,
                    size_code="source_size_invalid",
                    changed_code="source_changed",
                    unavailable_code="source_unavailable",
                )
                if expected_sha256 is not None and digest != expected_sha256:
                    raise UploadError("source_hash_mismatch")
                try:
                    target_before = _plain(target)
                    target_total, target_digest = self._stable_digest(
                        target,
                        target_before,
                        maximum=MAX_SOURCE_BYTES,
                        size_code="source_import_conflict",
                        changed_code="source_import_conflict",
                        unavailable_code="source_import_conflict",
                    )
                except (OSError, UploadError):
                    raise UploadError("source_import_conflict") from None
                if target_total != total or target_digest != digest:
                    raise UploadError("source_import_conflict")
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "INSERT INTO sources(id,name,suffix,size,sha256,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (source_id, name, suffix, total, digest, _now()),
                    )
                    row = db.execute(
                        self._source_query() + " WHERE s.id=?", (source_id,)
                    ).fetchone()
                return self._source_public(row)

            if shutil.disk_usage(self.root).free < before.st_size + UPLOAD_RESERVE_BYTES:
                raise UploadError("upload_storage_full")
            stage = media_root / f".{source_id}.{uuid4().hex}.import"
            digest_builder, total = hashlib.sha256(), 0
            published: Path | None = None
            try:
                with path.open("rb") as src, stage.open("xb") as dst:
                    if file_signature(os.fstat(src.fileno())) != file_signature(before):
                        raise UploadError("source_changed")
                    if os.name != "nt":
                        os.chmod(stage, 0o600)
                    while chunk := src.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise UploadError("source_size_invalid")
                        dst.write(chunk)
                        digest_builder.update(chunk)
                    if file_signature(os.fstat(src.fileno())) != file_signature(before):
                        raise UploadError("source_changed")
                    dst.flush()
                    os.fsync(dst.fileno())
                if total != before.st_size or file_signature(_plain(path)) != file_signature(before):
                    raise UploadError("source_changed")
                digest = digest_builder.hexdigest()
                if expected_sha256 is not None and digest != expected_sha256:
                    raise UploadError("source_hash_mismatch")
                if _plain(stage).st_size != total:
                    raise UploadError("source_unavailable")
                try:
                    os.link(stage, target)
                except FileExistsError:
                    raise UploadError("source_import_conflict") from None
                except OSError:
                    raise UploadError("source_import_failed") from None
                published = target
                stage.unlink()
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "INSERT INTO sources(id,name,suffix,size,sha256,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (source_id, name, suffix, total, digest, _now()),
                    )
                    row = db.execute(
                        self._source_query() + " WHERE s.id=?", (source_id,)
                    ).fetchone()
                published = None
                return self._source_public(row)
            finally:
                stage.unlink(missing_ok=True)
                if published is not None:
                    published.unlink(missing_ok=True)

    @_requires_activity
    def restore_source_media(self, source_id: str, path: Path) -> dict:
        """Restore an existing source ID only after exact byte-level verification."""
        source_id = _identifier(source_id)
        with self._db() as db:
            row = db.execute(self._source_query() + " WHERE s.id=?", (source_id,)).fetchone()
        if row is None:
            raise UploadError("source_not_found")
        state, _actual_size = self._source_media_state(row)
        if state == "present":
            raise UploadError("source_already_present")
        if state not in {"missing", "deleted"}:
            raise UploadError("source_changed")
        try:
            before = _plain(path)
        except OSError:
            raise UploadError("source_unavailable") from None
        if before.st_size != row["size"]:
            raise UploadError("source_restore_mismatch")
        if shutil.disk_usage(self.root).free < before.st_size + UPLOAD_RESERVE_BYTES:
            raise UploadError("upload_storage_full")
        media_root = self.root / "media"
        _plain(media_root, directory=True)
        stage = media_root / f".{source_id}.{uuid4().hex}.restore"
        digest = hashlib.sha256()
        total = 0
        published: Path | None = None
        try:
            with path.open("rb") as src, stage.open("xb") as dst:
                if file_signature(os.fstat(src.fileno())) != file_signature(before):
                    raise UploadError("source_changed")
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
                if file_signature(os.fstat(src.fileno())) != file_signature(before):
                    raise UploadError("source_changed")
                dst.flush()
                os.fsync(dst.fileno())
            if (total != row["size"] or digest.hexdigest() != row["sha256"]
                    or file_signature(_plain(path)) != file_signature(before)):
                raise UploadError("source_restore_mismatch")
            with self._active_guard, self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(self._source_query() + " WHERE s.id=?", (source_id,)).fetchone()
                if current is None:
                    raise UploadError("source_not_found")
                if self._source_media_state(current)[0] not in {"missing", "deleted"}:
                    raise UploadError("source_restore_conflict")
                target = self._source_media_path(current)
                try:
                    os.link(stage, target)
                except FileExistsError:
                    raise UploadError("source_restore_conflict") from None
                except OSError:
                    raise UploadError("source_restore_failed") from None
                published = target
                stage.unlink()
                db.execute(
                    "UPDATE sources SET media_state='present',deleted_at=NULL WHERE id=?",
                    (source_id,),
                )
                restored = db.execute(self._source_query() + " WHERE s.id=?", (source_id,)).fetchone()
                result = self._source_public(restored)
            published = None
            return result
        finally:
            stage.unlink(missing_ok=True)
            if published is not None:
                published.unlink(missing_ok=True)

    def _source_path(self, source_id: str) -> Path:
        _identifier(source_id)
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise UploadError("source_not_found")
        return self._verified_source_media_path(row)

    def _job_public(self, row) -> dict:
        result = dict(row)
        result.pop("_page_bucket", None)
        result.pop("_page_rowid", None)
        suffix = result.pop("source_suffix", None)
        recorded_media_state = result.pop("source_media_record_state", "present")
        result["tags"] = json.loads(result["tags"])
        result["platform_options"] = json.loads(result.get("platform_options", "{}"))
        if suffix is not None:
            state, _actual_size = self._source_media_state(
                {"id": result["source_id"], "suffix": suffix, "size": result["source_size"],
                 "media_state": recorded_media_state}
            )
            result["source_media_present"] = state == "present"
            result["source_media_state"] = state
        return result

    @staticmethod
    def _job_query() -> str:
        return ("SELECT j.*,a.platform,a.name AS account_name,"
                "a.lifecycle_state AS account_lifecycle_state,s.name AS source_name,"
                "s.size AS source_size,s.sha256 AS source_sha256,s.suffix AS source_suffix,"
                "s.media_state AS source_media_record_state FROM jobs j "
                "JOIN accounts a ON a.id=j.account_id JOIN sources s ON s.id=j.source_id")

    def job_page(self, *, cursor: str | None = None, limit: int = 50) -> dict:
        key = self._page_key(cursor, limit)
        query = ("SELECT * FROM (SELECT j.*,a.platform,a.name AS account_name,"
                 "a.lifecycle_state AS account_lifecycle_state,"
                 "s.name AS source_name,s.size AS source_size,s.sha256 AS source_sha256,"
                 "s.suffix AS source_suffix,s.media_state AS source_media_record_state,"
                 "j.rowid AS _page_rowid," + _JOB_PRIORITY
                 + " AS _page_bucket FROM jobs j JOIN accounts a ON a.id=j.account_id "
                   "JOIN sources s ON s.id=j.source_id) page")
        parameters: list[int] = []
        if key is not None:
            query += " WHERE (_page_bucket>? OR (_page_bucket=? AND _page_rowid<?))"
            parameters.extend((key[0], key[0], key[1]))
        query += " ORDER BY _page_bucket,_page_rowid DESC LIMIT ?"
        parameters.append(limit + 1)
        with self._db() as db:
            rows = list(db.execute(query, parameters))
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = f"{selected[-1]['_page_bucket']}:{selected[-1]['_page_rowid']}"
        return {"items": [self._job_public(row) for row in selected],
                "next_cursor": next_cursor}

    def jobs(self) -> list[dict]:
        return self.job_page(limit=200)["items"]

    def job(self, job_id: str) -> dict:
        with self._db() as db:
            return self._get_job(db, job_id)

    def jobs_by_ids(self, job_ids: list[str]) -> list[dict]:
        if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 64
                or len(set(job_ids)) != len(job_ids)):
            raise UploadError("invalid_job_ids")
        for job_id in job_ids:
            _identifier(job_id)
        placeholders = ",".join("?" for _ in job_ids)
        with self._db() as db:
            rows = db.execute(self._job_query() + f" WHERE j.id IN ({placeholders})", job_ids)
            records = {row["id"]: self._job_public(row) for row in rows}
        return [records[job_id] for job_id in job_ids if job_id in records]

    def latest_jobs_by_ids(self, job_ids: list[str]) -> list[dict]:
        """Resolve each job through its unique retry chain to the current leaf."""

        if (
            not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 64
            or len(set(job_ids)) != len(job_ids)
        ):
            raise UploadError("invalid_job_ids")
        normalized = [_identifier(job_id) for job_id in job_ids]
        leaves: list[dict] = []
        with self._db() as db:
            for root_id in normalized:
                root = self._get_job(db, root_id)
                current = root
                seen = {root_id}
                while True:
                    successors = list(
                        db.execute(
                            "SELECT id FROM jobs WHERE retry_of=? ORDER BY rowid",
                            (current["id"],),
                        )
                    )
                    if not successors:
                        break
                    if len(successors) != 1:
                        raise UploadError("job_retry_lineage_invalid")
                    successor_id = _identifier(successors[0]["id"])
                    if successor_id in seen:
                        raise UploadError("job_retry_lineage_invalid")
                    seen.add(successor_id)
                    successor = self._get_job(db, successor_id)
                    if any(
                        successor[field] != root[field]
                        for field in ("account_id", "source_id", "platform")
                    ):
                        raise UploadError("job_retry_lineage_invalid")
                    current = successor
                leaves.append(current)
        if len({job["id"] for job in leaves}) != len(leaves):
            raise UploadError("job_retry_lineage_invalid")
        return leaves

    def jobs_for_request(
        self,
        idempotency_key: str,
        *,
        expected_request: Mapping[str, object] | None = None,
    ) -> list[dict] | None:
        """Read the immutable roots created by one idempotent fan-out request."""

        if (
            not isinstance(idempotency_key, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key) is None
        ):
            raise UploadError("invalid_idempotency_key")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest,job_ids,digest_version FROM requests WHERE id=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return None
            expected_targets: list[dict] | None = None
            expected_source_id: str | None = None
            if expected_request is not None:
                try:
                    expected_source_id, expected_targets, expected_digest = (
                        self._workflow_request_identity_in(db, expected_request)
                    )
                except (KeyError, TypeError, UploadError, ValueError):
                    raise UploadError("upload_request_invalid") from None
                if (
                    row["digest_version"] != 2
                    or not isinstance(row["digest"], str)
                    or _SHA256.fullmatch(row["digest"]) is None
                    or not hmac.compare_digest(row["digest"], expected_digest)
                ):
                    raise UploadError("upload_request_mismatch")
            return self._request_jobs_from_row_in(
                db,
                row,
                expected_source_id=expected_source_id,
                expected_targets=expected_targets,
            )

    @_requires_activity
    def claim_workflow_request_for_cancellation(
        self,
        idempotency_key: str,
        *,
        expected_request: Mapping[str, object],
    ) -> list[dict] | None:
        """Atomically discover a workflow fan-out or tombstone its stable key."""

        if (
            not isinstance(idempotency_key, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key) is None
        ):
            raise UploadError("invalid_idempotency_key")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                source_id, targets, expected_digest = (
                    self._workflow_request_identity_in(db, expected_request)
                )
            except (KeyError, TypeError, UploadError, ValueError):
                raise UploadError("upload_request_invalid") from None
            row = db.execute(
                "SELECT digest,job_ids,digest_version FROM requests WHERE id=?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO requests(id,digest,job_ids,digest_version) "
                    "VALUES(?,?,?,2)",
                    (idempotency_key, expected_digest, "[]"),
                )
                return None
            if (
                row["digest_version"] != 2
                or not isinstance(row["digest"], str)
                or _SHA256.fullmatch(row["digest"]) is None
                or not hmac.compare_digest(row["digest"], expected_digest)
            ):
                raise UploadError("upload_request_mismatch")
            try:
                existing_ids = json.loads(row["job_ids"])
            except (TypeError, ValueError):
                raise UploadError("upload_request_invalid") from None
            if existing_ids == []:
                return None
            return self._request_jobs_from_row_in(
                db,
                row,
                expected_source_id=source_id,
                expected_targets=targets,
            )

    def _request_jobs_from_row_in(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_source_id: str | None = None,
        expected_targets: Sequence[Mapping[str, object]] | None = None,
    ) -> list[dict]:
        try:
            job_ids = json.loads(row["job_ids"])
        except (TypeError, ValueError):
            raise UploadError("upload_request_invalid") from None
        if (
            row["digest_version"] not in {1, 2}
            or not isinstance(row["digest"], str)
            or _SHA256.fullmatch(row["digest"]) is None
            or not isinstance(job_ids, list)
            or not 1 <= len(job_ids) <= 20
            or any(
                not isinstance(job_id, str) or _ID.fullmatch(job_id) is None
                for job_id in job_ids
            )
            or len(set(job_ids)) != len(job_ids)
        ):
            raise UploadError("upload_request_invalid")
        try:
            jobs = [self._get_job(db, job_id) for job_id in job_ids]
        except (json.JSONDecodeError, TypeError, UploadError) as error:
            if isinstance(error, (json.JSONDecodeError, TypeError)):
                raise UploadError("upload_request_invalid") from None
            if error.code == "job_not_found":
                raise UploadError("upload_request_invalid") from None
            raise
        if expected_targets is not None and expected_source_id is not None:
            if len(jobs) != len(expected_targets) or any(
                job.get("source_id") != expected_source_id
                or any(
                    job.get(field) != target[field]
                    for field in (
                        "account_id",
                        "platform",
                        "title",
                        "description",
                        "tags",
                        "category_id",
                        "mode",
                        "copyright",
                        "source_credit",
                        "cover_landscape_asset_id",
                        "cover_portrait_asset_id",
                        "publish_at_unix",
                        "publish_timezone_offset_minutes",
                        "platform_options",
                    )
                )
                for job, target in zip(jobs, expected_targets, strict=True)
            ):
                raise UploadError("upload_request_mismatch")
        return jobs

    def _workflow_request_identity_in(
        self, db: sqlite3.Connection, expected: Mapping[str, object]
    ) -> tuple[str, list[dict], str]:
        source_id, targets = self._workflow_request_targets_in(db, expected)
        digest_payload = {
            "source_id": source_id,
            "targets": sorted(targets, key=lambda item: item["account_id"]),
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return source_id, targets, digest

    def _workflow_request_targets_in(
        self, db: sqlite3.Connection, expected: Mapping[str, object]
    ) -> tuple[str, list[dict]]:
        expected_keys = {
            "source_id",
            "account_ids",
            "title",
            "description",
            "tags",
            "category_id",
            "mode",
            "copyright",
            "source_credit",
            "target_overrides",
        }
        if not isinstance(expected, Mapping) or set(expected) != expected_keys:
            raise UploadError("upload_request_invalid")
        source_id = _identifier(expected["source_id"])
        account_ids = expected["account_ids"]
        if (
            not isinstance(account_ids, Sequence)
            or isinstance(account_ids, (str, bytes))
            or not 1 <= len(account_ids) <= 20
        ):
            raise UploadError("upload_request_invalid")
        normalized_account_ids = [_identifier(item) for item in account_ids]
        if len(set(normalized_account_ids)) != len(normalized_account_ids):
            raise UploadError("upload_request_invalid")
        title = normalize_upload_text(expected["title"], 100, required=True)
        description = normalize_upload_text(expected["description"], 2000)
        tags = normalize_upload_tags(expected["tags"])
        category_id = expected["category_id"]
        mode = expected["mode"]
        copyright_value = expected["copyright"]
        source_credit = normalize_upload_text(expected["source_credit"], 200)
        if (
            mode not in {"publish", "draft"}
            or category_id is not None
            and (type(category_id) is not int or not 1 <= category_id <= 10_000)
            or copyright_value is not None
            and (type(copyright_value) is not int or copyright_value not in {1, 2})
        ):
            raise UploadError("upload_request_invalid")
        raw_overrides = expected["target_overrides"]
        if (
            not isinstance(raw_overrides, Sequence)
            or isinstance(raw_overrides, (str, bytes))
            or len(raw_overrides) > len(normalized_account_ids)
        ):
            raise UploadError("upload_request_invalid")
        override_map: dict[str, dict] = {}
        for raw in raw_overrides:
            if (
                not isinstance(raw, Mapping)
                or set(raw) - TARGET_OVERRIDE_KEYS
                or set(raw) == {"account_id"}
            ):
                raise UploadError("upload_request_invalid")
            account_id = _identifier(raw.get("account_id"))
            if account_id not in normalized_account_ids or account_id in override_map:
                raise UploadError("upload_request_invalid")
            override_map[account_id] = dict(raw)
        base = {
            "title": title,
            "description": description,
            "tags": tags,
            "category_id": category_id,
            "mode": mode,
            "copyright": copyright_value,
            "source_credit": source_credit,
            "cover_landscape_asset_id": None,
            "cover_portrait_asset_id": None,
            "publish_at_unix": None,
            "publish_timezone_offset_minutes": None,
            "platform_options": {},
        }
        targets: list[dict] = []
        for account_id in normalized_account_ids:
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise UploadError("upload_request_invalid")
            targets.append(
                self._normalize_target(
                    db=db,
                    account=account,
                    base=base,
                    override=override_map.get(account_id, {}),
                    verify_assets=False,
                    enforce_schedule=False,
                )
            )
        return source_id, targets

    def _get_job(self, db, job_id: str) -> dict:
        row = db.execute(self._job_query() + " WHERE j.id=?", (_identifier(job_id),)).fetchone()
        if row is None:
            raise UploadError("job_not_found")
        return self._job_public(row)

    @staticmethod
    def _normalize_v2_replay_tags(tags) -> list[str]:
        """Map only a pre-Schema-3 request onto its migrated stored tags."""
        if not isinstance(tags, list) or len(tags) > 10:
            raise UploadError("invalid_tags")
        legacy = [normalize_upload_text(tag, 20, required=True) for tag in tags]
        if (len(set(legacy)) != len(legacy)
                or any(any(c in tag for c in ",\n\r\t") for tag in legacy)):
            raise UploadError("invalid_tags")
        normalized: list[str] = []
        for tag in legacy:
            while tag[:1] in {"#", "＃"}:
                tag = tag[1:].lstrip()
            tag = tag.replace("#", "井").replace("＃", "井").replace("，", "、") or "井"
            if tag not in normalized:
                normalized.append(tag)
        return normalized

    def _verified_cover_row(self, db, asset_id: str | None):
        if asset_id is None:
            return None
        row = db.execute(self._asset_query() + " WHERE u.id=? AND u.kind='cover'",
                         (_identifier(asset_id),)).fetchone()
        if row is None:
            raise UploadError("cover_not_found")
        self._verified_asset_media_path(row)
        return row

    def _verified_job_cover_rows(
        self,
        db,
        platform: str,
        landscape_id: str | None,
        portrait_id: str | None,
    ):
        if portrait_id is not None and platform not in {"douyin", "tencent"}:
            raise UploadError("portrait_cover_unsupported")
        if platform == "douyin" and landscape_id is not None and portrait_id is not None:
            raise UploadError("multiple_covers_unsupported")
        landscape = self._verified_cover_row(db, landscape_id)
        portrait = self._verified_cover_row(db, portrait_id)
        if (
            platform == "bilibili"
            and landscape is not None
            and landscape["width"] < landscape["height"]
        ):
            raise UploadError("bilibili_cover_orientation_invalid")
        if platform == "douyin" and (
            landscape is not None and landscape["width"] < landscape["height"]
            or portrait is not None and portrait["height"] <= portrait["width"]
        ):
            raise UploadError("douyin_cover_orientation_invalid")
        if platform == "tencent":
            for row, expected in ((landscape, 4 / 3), (portrait, 3 / 4)):
                if row is not None and abs(row["width"] / row["height"] - expected) > 0.04:
                    raise UploadError("tencent_cover_ratio_invalid")
        return landscape, portrait

    def _normalize_target(self, *, db, account, base: dict, override: dict,
                          verify_assets: bool = True, enforce_schedule: bool = True,
                          accept_normalized_short_title: bool = False) -> dict:
        platform = account["platform"]
        if platform != "bilibili" and any(
            key in override for key in ("category_id", "copyright", "source_credit")
        ):
            raise UploadError("unsupported_platform_field")
        raw = {**base, **{key: value for key, value in override.items() if key != "account_id"}}
        title = normalize_upload_text(raw["title"], 100, required=True)
        if len(title) > TITLE_LIMITS[platform]:
            raise UploadError("title_too_long")
        description = normalize_upload_text(raw.get("description", ""), 2000)
        tags = normalize_upload_tags(raw.get("tags", []))
        mode = raw.get("mode", "publish")
        if mode not in {"publish", "draft"}:
            raise UploadError("invalid_metadata")
        if mode == "draft" and platform != "tencent":
            raise UploadError("draft_mode_unsupported")

        category_id = raw.get("category_id")
        copyright = raw.get("copyright")
        source_credit = normalize_upload_text(raw.get("source_credit", ""), 200)
        if category_id is not None and (
            type(category_id) is not int or not 1 <= category_id <= 10000
        ):
            raise UploadError("invalid_metadata")
        if copyright is not None and (type(copyright) is not int or copyright not in {1, 2}):
            raise UploadError("invalid_metadata")
        if platform == "bilibili":
            if category_id is None:
                raise UploadError("bilibili_category_required")
            if copyright is None:
                raise UploadError("bilibili_copyright_required")
            if not tags:
                raise UploadError("bilibili_tags_required")
            if copyright == 2 and not source_credit:
                raise UploadError("source_credit_required")
            if copyright == 1 and source_credit:
                raise UploadError("source_credit_not_allowed")
        else:
            category_id, copyright, source_credit = None, 1, ""

        landscape_id = raw.get("cover_landscape_asset_id")
        portrait_id = raw.get("cover_portrait_asset_id")
        if landscape_id is not None:
            _identifier(landscape_id)
        if portrait_id is not None:
            _identifier(portrait_id)
        if verify_assets:
            self._verified_job_cover_rows(db, platform, landscape_id, portrait_id)
        else:
            if portrait_id is not None and platform not in {"douyin", "tencent"}:
                raise UploadError("portrait_cover_unsupported")
            if platform == "douyin" and landscape_id is not None and portrait_id is not None:
                raise UploadError("multiple_covers_unsupported")

        publish_at, offset = validate_publish_schedule(
            platform, raw.get("publish_at_unix"), raw.get("publish_timezone_offset_minutes"),
            now=int(time.time()) if enforce_schedule else 0,
            enforce_tencent_horizon=enforce_schedule,
        )
        if mode == "draft" and publish_at is not None:
            raise UploadError("draft_schedule_unsupported")
        options = normalize_platform_options(platform, raw.get("platform_options"))
        if platform == "tencent":
            short_title = options["short_title"]
            options["short_title"] = (
                short_title
                if accept_normalized_short_title
                and is_tencent_short_title_output(short_title)
                else normalize_tencent_short_title(
                    short_title if short_title is not None else title
                )
            )
        return {
            "account_id": account["id"], "platform": platform, "title": title,
            "description": description, "tags": tags, "category_id": category_id,
            "mode": mode, "copyright": copyright, "source_credit": source_credit,
            "cover_landscape_asset_id": landscape_id,
            "cover_portrait_asset_id": portrait_id, "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": offset, "platform_options": options,
        }

    @staticmethod
    def _workflow_account_binding(
        db: sqlite3.Connection, account: sqlite3.Row
    ) -> dict[str, str]:
        latest_login = db.execute(
            "SELECT id FROM operations WHERE account_id=? AND action='login' "
            "ORDER BY rowid DESC LIMIT 1",
            (account["id"],),
        ).fetchone()
        return {
            "account_id": account["id"],
            "platform": account["platform"],
            # The account id is generation zero. Every login attempt creates a
            # durable operation id before it can alter the local browser session.
            "session_revision": (
                account["id"] if latest_login is None else latest_login["id"]
            ),
        }

    def _validate_workflow_account_bindings(
        self,
        db: sqlite3.Connection,
        accounts_by_id: Mapping[str, sqlite3.Row],
        expected: list[dict] | None,
        *,
        verify_account_ids: set[str] | None = None,
        verify_current_session: bool = True,
    ) -> None:
        if expected is None:
            return
        if (
            not isinstance(expected, list)
            or len(expected) != len(accounts_by_id)
            or any(
                not isinstance(item, dict)
                for item in expected
            )
        ):
            raise UploadError("invalid_account_bindings")
        normalized = normalize_account_bindings(expected)
        by_id = {binding["account_id"]: binding for binding in normalized}
        if any(account_id not in accounts_by_id for account_id in by_id):
            raise UploadError("invalid_account_bindings")
        account_ids = set(accounts_by_id)
        if verify_account_ids is None:
            verify_account_ids = account_ids
        elif not isinstance(verify_account_ids, set) or not verify_account_ids <= account_ids:
            raise UploadError("invalid_account_bindings")
        for account_id in verify_account_ids:
            account = accounts_by_id[account_id]
            expected_binding = by_id.get(account_id)
            current_binding = self._workflow_account_binding(db, account)
            if expected_binding is None or (
                expected_binding != current_binding
                if verify_current_session
                else expected_binding["platform"] != current_binding["platform"]
            ):
                raise UploadError("account_session_changed")

    @_requires_activity
    def preflight_jobs(
        self,
        *,
        account_ids: list[str],
        title: str,
        description: str,
        tags: list[str],
        category_id: int | None = None,
        mode: str = "publish",
        copyright: int | None = None,
        source_credit: str = "",
        target_overrides: list[dict] | None = None,
        expected_account_bindings: list[dict] | None = None,
    ) -> list[dict]:
        """Validate workflow upload metadata before download or AI work begins."""

        if (
            not isinstance(account_ids, list)
            or not 1 <= len(account_ids) <= 20
            or len(set(account_ids)) != len(account_ids)
        ):
            raise UploadError("invalid_accounts")
        for account_id in account_ids:
            _identifier(account_id)
        base = {
            "title": normalize_upload_text(title, 100, required=True),
            "description": normalize_upload_text(description, 2000),
            "tags": normalize_upload_tags(tags),
            "category_id": category_id,
            "mode": mode,
            "copyright": copyright,
            "source_credit": normalize_upload_text(source_credit, 200),
            "cover_landscape_asset_id": None,
            "cover_portrait_asset_id": None,
            "publish_at_unix": None,
            "publish_timezone_offset_minutes": None,
            "platform_options": {},
        }
        if (
            mode not in {"publish", "draft"}
            or (
                category_id is not None
                and (type(category_id) is not int or not 1 <= category_id <= 10_000)
            )
            or (
                copyright is not None
                and (type(copyright) is not int or copyright not in {1, 2})
            )
        ):
            raise UploadError("invalid_metadata")
        target_overrides = [] if target_overrides is None else target_overrides
        if not isinstance(target_overrides, list) or len(target_overrides) > len(account_ids):
            raise UploadError("invalid_target_overrides")
        override_map: dict[str, dict] = {}
        for override in target_overrides:
            if (
                not isinstance(override, dict)
                or set(override) - TARGET_OVERRIDE_KEYS
                or set(override) == {"account_id"}
            ):
                raise UploadError("invalid_target_override")
            account_id = _identifier(override.get("account_id"))
            if account_id not in account_ids or account_id in override_map:
                raise UploadError("invalid_target_override")
            override_map[account_id] = override
        with self._db() as db:
            # Keep account readiness and the durable login generation in one
            # snapshot.  A concurrent login must either finish before this
            # preflight or start after it; it cannot be spliced between the two
            # reads and accidentally bless a different browser session.
            db.execute("BEGIN IMMEDIATE")
            targets: list[dict] = []
            accounts_by_id: dict[str, sqlite3.Row] = {}
            for account_id in account_ids:
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise UploadError("account_not_found")
                accounts_by_id[account_id] = account
            self._validate_workflow_account_bindings(
                db, accounts_by_id, expected_account_bindings
            )
            for account_id in account_ids:
                account = accounts_by_id[account_id]
                if account["lifecycle_state"] != "active":
                    raise UploadError("account_disconnected")
                if account["auth_state"] != "ready":
                    raise UploadError("account_not_ready")
                if account["platform"] not in PLATFORMS:
                    raise UploadError("invalid_platform")
                target = self._normalize_target(
                    db=db,
                    account=account,
                    base=base,
                    override=override_map.get(account_id, {}),
                    verify_assets=True,
                )
                target["account_binding"] = self._workflow_account_binding(
                    db, account
                )
                targets.append(target)
        self._require_workflow_runtime(
            execution_check=expected_account_bindings is not None
        )
        return targets

    @_requires_activity
    def create_jobs(self, *, source_id: str, account_ids: list[str], title: str,
                    description: str, tags: list[str], idempotency_key: str,
                    category_id: int | None = None, mode: str = "publish",
                    copyright: int | None = None, source_credit: str = "",
                    target_overrides: list[dict] | None = None,
                    expected_account_bindings: list[dict] | None = None) -> list[dict]:
        _identifier(source_id)
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 20 or len(set(account_ids)) != len(account_ids):
            raise UploadError("invalid_accounts")
        for account_id in account_ids:
            _identifier(account_id)
        title = normalize_upload_text(title, 100, required=True)
        description = normalize_upload_text(description, 2000)
        source_credit = normalize_upload_text(source_credit, 200)
        raw_tags = tags
        try:
            tags = normalize_upload_tags(tags)
            tags_error = None
        except UploadError as exc:
            tags = None
            tags_error = exc
        if (
            mode not in ("publish", "draft")
            or (
                category_id is not None
                and (type(category_id) is not int or not 1 <= category_id <= 10000)
            )
            or (
                copyright is not None
                and (type(copyright) is not int or copyright not in (1, 2))
            )
        ):
            raise UploadError("invalid_metadata")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", idempotency_key):
            raise UploadError("invalid_idempotency_key")
        if target_overrides is None:
            target_overrides = []
        if not isinstance(target_overrides, list) or len(target_overrides) > len(account_ids):
            raise UploadError("invalid_target_overrides")
        override_map: dict[str, dict] = {}
        for override in target_overrides:
            if (not isinstance(override, dict) or set(override) - TARGET_OVERRIDE_KEYS
                    or set(override) == {"account_id"}):
                raise UploadError("invalid_target_override")
            account_id = _identifier(override.get("account_id"))
            if account_id not in account_ids or account_id in override_map:
                raise UploadError("invalid_target_override")
            override_map[account_id] = override
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM requests WHERE id=?", (idempotency_key,)).fetchone()
            if prior is not None:
                try:
                    prior_job_ids = json.loads(prior["job_ids"])
                except (TypeError, ValueError):
                    raise UploadError("idempotency_conflict") from None
                if prior_job_ids == []:
                    raise UploadError("idempotency_conflict")
            accounts_by_id = {}
            for account_id in account_ids:
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise UploadError("account_not_found")
                accounts_by_id[account_id] = account
            self._validate_workflow_account_bindings(
                db, accounts_by_id, expected_account_bindings
            )
            if prior:
                if prior["digest_version"] == 1:
                    replay_tags = (
                        tags if tags_error is None else self._normalize_v2_replay_tags(raw_tags)
                    )
                    legacy_source_credit = (
                        ""
                        if copyright == 1 and any(
                            account["platform"] == "bilibili"
                            for account in accounts_by_id.values()
                        )
                        else source_credit
                    )
                    legacy_payload = [
                        source_id, sorted(account_ids), title, description, replay_tags,
                        category_id, mode, copyright, legacy_source_credit,
                    ]
                    legacy_digest = hashlib.sha256(json.dumps(
                        legacy_payload, ensure_ascii=False, separators=(",", ":")
                    ).encode()).hexdigest()
                    if target_overrides or prior["digest"] != legacy_digest:
                        raise UploadError("idempotency_conflict")
                elif prior["digest_version"] != 2:
                    raise UploadError("idempotency_conflict")
                else:
                    if tags_error is not None:
                        raise tags_error
                    assert tags is not None
                    base = {
                        "title": title, "description": description, "tags": tags,
                        "category_id": category_id, "mode": mode,
                        "copyright": copyright, "source_credit": source_credit,
                        "cover_landscape_asset_id": None,
                        "cover_portrait_asset_id": None, "publish_at_unix": None,
                        "publish_timezone_offset_minutes": None,
                        "platform_options": {},
                    }
                    replay_targets = [self._normalize_target(
                        db=db, account=accounts_by_id[account_id], base=base,
                        override=override_map.get(account_id, {}), verify_assets=False,
                        enforce_schedule=False,
                    ) for account_id in account_ids]
                    digest_payload = {"source_id": source_id, "targets": sorted(
                        replay_targets, key=lambda item: item["account_id"]
                    )}
                    replay_digest = hashlib.sha256(json.dumps(
                        digest_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    ).encode()).hexdigest()
                    if prior["digest"] != replay_digest:
                        raise UploadError("idempotency_conflict")
                if prior["digest_version"] not in {1, 2}:
                    raise UploadError("idempotency_conflict")
                return [self._get_job(db, job_id) for job_id in prior_job_ids]
            if tags_error is not None:
                raise tags_error
            assert tags is not None
            base = {
                "title": title, "description": description, "tags": tags,
                "category_id": category_id, "mode": mode, "copyright": copyright,
                "source_credit": source_credit, "cover_landscape_asset_id": None,
                "cover_portrait_asset_id": None, "publish_at_unix": None,
                "publish_timezone_offset_minutes": None, "platform_options": {},
            }
            source = db.execute(self._source_query() + " WHERE s.id=?", (source_id,)).fetchone()
            if source is None:
                raise UploadError("source_not_found")
            self._verified_source_media_path(source)
            targets = []
            for account_id in account_ids:
                account = accounts_by_id[account_id]
                if account["lifecycle_state"] != "active":
                    raise UploadError("account_disconnected")
                if account["platform"] not in PLATFORMS:
                    raise UploadError("invalid_platform")
                targets.append(self._normalize_target(
                    db=db, account=account, base=base,
                    override=override_map.get(account_id, {}),
                ))
            digest_payload = {"source_id": source_id,
                              "targets": sorted(targets, key=lambda item: item["account_id"])}
            digest = hashlib.sha256(json.dumps(
                digest_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()).hexdigest()
            job_ids, now = [], _now()
            for target in targets:
                job_id = uuid4().hex
                job_ids.append(job_id)
                db.execute(
                    "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
                    "mode,copyright,source_credit,created_at,updated_at,cover_landscape_asset_id,"
                    "cover_portrait_asset_id,publish_at_unix,publish_timezone_offset_minutes,"
                    "platform_options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, target["account_id"], source_id, target["title"],
                     target["description"], json.dumps(target["tags"], ensure_ascii=False),
                     target["category_id"], target["mode"], target["copyright"],
                     target["source_credit"], now, now, target["cover_landscape_asset_id"],
                     target["cover_portrait_asset_id"], target["publish_at_unix"],
                     target["publish_timezone_offset_minutes"], json.dumps(
                         target["platform_options"], ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True,
                     )),
                )
            db.execute("INSERT INTO requests(id,digest,job_ids,digest_version) VALUES(?,?,?,2)",
                       (idempotency_key, digest, json.dumps(job_ids)))
            return [self._get_job(db, job_id) for job_id in job_ids]

    @_requires_activity
    def confirm(self, job_id: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] in ("queued", "running", "submitted", "draft_saved"):
                return job
            self._validate_confirmation(db, job)
            db.execute("UPDATE jobs SET state='queued',code='',updated_at=? WHERE id=?", (_now(), job_id))
            result = self._get_job(db, job_id)
        self._wake.set()
        return result

    @_requires_activity
    def confirm_many(
        self,
        job_ids: tuple[str, ...],
        *,
        expected_account_bindings: list[dict] | None = None,
    ) -> list[dict]:
        """Validate and queue one workflow's upload drafts atomically."""

        if (
            not isinstance(job_ids, tuple)
            or not job_ids
            or len(job_ids) > 32
            or len(set(job_ids)) != len(job_ids)
        ):
            raise UploadError("invalid_job_batch")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            jobs = [self._get_job(db, job_id) for job_id in job_ids]
            account_ids = {job["account_id"] for job in jobs}
            accounts_by_id = {
                account_id: db.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                for account_id in account_ids
            }
            if any(account is None for account in accounts_by_id.values()):
                raise UploadError("account_not_found")
            pending = [job for job in jobs if job["state"] == "draft"]
            self._validate_workflow_account_bindings(
                db,
                accounts_by_id,
                expected_account_bindings,
                verify_account_ids={job["account_id"] for job in pending},
            )
            if any(
                job["state"]
                not in ("draft", "queued", "running", "submitted", "draft_saved")
                for job in jobs
            ):
                raise UploadError("job_requires_new_draft")
            for job in pending:
                self._validate_confirmation(db, job)
            now = _now()
            for job in pending:
                db.execute(
                    "UPDATE jobs SET state='queued',code='',updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
            result = [self._get_job(db, job_id) for job_id in job_ids]
        if pending:
            self._wake.set()
        return result

    def _validate_confirmation(self, db: sqlite3.Connection, job: dict) -> None:
        if job["state"] != "draft":
            raise UploadError("job_requires_new_draft")
        if not self.backend.inspect().get("ready"):
            raise UploadError("runtime_missing")
        account = db.execute(
            "SELECT * FROM accounts WHERE id=?",
            (job["account_id"],),
        ).fetchone()
        if account is None:
            raise UploadError("account_not_found")
        if account["lifecycle_state"] != "active":
            raise UploadError("account_disconnected")
        if account["auth_state"] != "ready":
            raise UploadError("account_not_ready")
        # A draft may survive an application upgrade or be restored from an
        # older database.  Re-run the current per-platform contract before
        # queueing so a pre-authorized restart cannot dispatch metadata that
        # the running build would no longer accept.  Exact equality also makes
        # non-canonical or no-longer-supported persisted values fail closed.
        persisted_target = {
            "account_id": job["account_id"],
            **{
                key: job[key]
                for key in TARGET_OVERRIDE_KEYS
                if key != "account_id"
            },
        }
        normalized_target = self._normalize_target(
            db=db,
            account=account,
            base=persisted_target,
            override={},
            verify_assets=False,
            enforce_schedule=False,
            accept_normalized_short_title=True,
        )
        if normalized_target != {
            "platform": job["platform"],
            **persisted_target,
        }:
            raise UploadError("invalid_metadata")
        validate_publish_schedule(
            job["platform"],
            job["publish_at_unix"],
            job["publish_timezone_offset_minutes"],
            now=int(time.time()),
        )
        source = db.execute(
            "SELECT * FROM sources WHERE id=?", (job["source_id"],)
        ).fetchone()
        if source is None:
            raise UploadError("source_not_found")
        self._verified_source_media_path(source)
        self._verified_job_cover_rows(
            db,
            job["platform"],
            job["cover_landscape_asset_id"],
            job["cover_portrait_asset_id"],
        )

    def _cancel_current_jobs(
        self,
        db: sqlite3.Connection,
        job_ids: tuple[str, ...],
        expected_targets: tuple[dict[str, str], ...] | None,
        expected_account_bindings: list[dict] | None,
    ) -> tuple[list[dict], tuple[threading.Event, ...]]:
        jobs = [self._get_job(db, job_id) for job_id in job_ids]
        if expected_targets is not None and any(
            job["id"] != expected["job_id"]
            or any(
                job[field] != expected[field]
                for field in ("source_id", "account_id", "platform")
            )
            for job, expected in zip(jobs, expected_targets, strict=True)
        ):
            raise UploadError("invalid_job_batch")
        if expected_targets is not None and any(
            db.execute(
                "SELECT 1 FROM jobs WHERE retry_of=? LIMIT 1", (job["id"],)
            ).fetchone()
            is not None
            for job in jobs
        ):
            # The caller resolved these rows as retry leaves before entering
            # this transaction. A new successor means that view is stale; do
            # not report cancellation while a newer child remains active.
            raise UploadError("job_retry_lineage_changed")
        if expected_account_bindings is not None:
            account_ids = {job["account_id"] for job in jobs}
            accounts_by_id = {
                account_id: db.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                for account_id in account_ids
            }
            if any(account is None for account in accounts_by_id.values()):
                raise UploadError("account_not_found")
            self._validate_workflow_account_bindings(
                db,
                accounts_by_id,
                expected_account_bindings,
                verify_account_ids=set(accounts_by_id),
                # A login revision authorizes dispatch; it is not required to
                # stop immutable jobs whose exact target identity still
                # matches. Re-login must never make emergency cancellation
                # unavailable.
                verify_current_session=False,
            )

        now = _now()
        cancellable = [
            ("canceled", "canceled", now, job["id"])
            for job in jobs
            if job["state"] in ("draft", "queued")
        ]
        cancellation_requested = [
            ("cancellation_requested", now, job["id"])
            for job in jobs
            if job["state"] == "running"
        ]
        if cancellable:
            db.executemany(
                "UPDATE jobs SET state=?,code=?,updated_at=? WHERE id=?",
                cancellable,
            )
        if cancellation_requested:
            db.executemany(
                "UPDATE jobs SET code=?,updated_at=? WHERE id=?",
                cancellation_requested,
            )
        running_ids = {item[2] for item in cancellation_requested}
        stops = (
            (self._operation_stop,)
            if self._active_id is not None and self._active_id in running_ids
            else ()
        )
        return [self._get_job(db, job_id) for job_id in job_ids], stops

    @_requires_activity
    def cancel(self, job_id: str) -> dict:
        normalized_id = _identifier(job_id)
        with self._active_guard:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                jobs, stops = self._cancel_current_jobs(
                    db, (normalized_id,), None, None
                )
            for stop in stops:
                stop.set()
        return jobs[0]

    @_requires_activity
    def cancel_many(
        self,
        job_ids: Sequence[str],
        *,
        expected_targets: Sequence[Mapping[str, str]] | None = None,
        expected_account_bindings: Sequence[Mapping[str, str]] | None = None,
    ) -> list[dict]:
        """Cancel current upload jobs together without following retry lineage."""

        normalized_ids, normalized_targets = normalize_upload_job_batch(
            job_ids, expected_targets
        )
        if expected_account_bindings is None:
            normalized_bindings = None
        elif (
            not isinstance(expected_account_bindings, Sequence)
            or isinstance(expected_account_bindings, (str, bytes))
            or any(
                not isinstance(binding, Mapping)
                for binding in expected_account_bindings
            )
        ):
            raise UploadError("invalid_account_bindings")
        else:
            normalized_bindings = [
                dict(binding) for binding in expected_account_bindings
            ]
        with self._active_guard:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                jobs, stops = self._cancel_current_jobs(
                    db,
                    normalized_ids,
                    normalized_targets,
                    normalized_bindings,
                )
            # The transaction must commit before a backend can observe its stop
            # event and finalize the running job.
            for stop in stops:
                stop.set()
        return jobs

    @_requires_activity
    def retry(self, job_id: str, acknowledge_unknown: bool = False) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] not in ("failed", "canceled", "unknown"):
                raise UploadError("retry_not_allowed")
            if job["state"] == "unknown" and acknowledge_unknown is not True:
                raise UploadError("verify_remote_result_first")
            # Repeated requests for the same retry return the existing successor.
            successor = db.execute("SELECT id FROM jobs WHERE retry_of=? ORDER BY rowid LIMIT 1", (job_id,)).fetchone()
            if successor:
                return self._get_job(db, successor["id"])
            validate_publish_schedule(
                job["platform"], job["publish_at_unix"],
                job["publish_timezone_offset_minutes"],
                now=int(time.time()),
            )
            account = db.execute("SELECT lifecycle_state FROM accounts WHERE id=?",
                                 (job["account_id"],)).fetchone()
            if account is None or account["lifecycle_state"] != "active":
                raise UploadError("account_disconnected")
            source = db.execute("SELECT * FROM sources WHERE id=?", (job["source_id"],)).fetchone()
            if source is None:
                raise UploadError("source_not_found")
            self._verified_source_media_path(source)
            self._verified_job_cover_rows(
                db,
                job["platform"],
                job["cover_landscape_asset_id"],
                job["cover_portrait_asset_id"],
            )
            new_id, now = uuid4().hex, _now()
            db.execute(
                "INSERT INTO jobs(id,account_id,source_id,title,description,tags,category_id,"
                "mode,copyright,source_credit,created_at,updated_at,retry_of,"
                "cover_landscape_asset_id,cover_portrait_asset_id,publish_at_unix,"
                "publish_timezone_offset_minutes,platform_options) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, job["account_id"], job["source_id"], job["title"],
                 job["description"], json.dumps(job["tags"], ensure_ascii=False),
                 job["category_id"], job["mode"], job["copyright"], job["source_credit"],
                 now, now, job_id, job["cover_landscape_asset_id"],
                 job["cover_portrait_asset_id"], job["publish_at_unix"],
                 job["publish_timezone_offset_minutes"], json.dumps(
                     job["platform_options"], ensure_ascii=False,
                     separators=(",", ":"), sort_keys=True,
                 )),
            )
            return self._get_job(db, new_id)

    def _claim(self) -> tuple[str, dict] | None:
        with self._active_guard, self._db() as db:
            if self._shutdown.is_set():
                return None
            db.execute("BEGIN IMMEDIATE")
            operation = db.execute(
                "SELECT o.*,a.platform FROM operations o JOIN accounts a ON a.id=o.account_id "
                "WHERE o.state='queued' AND a.lifecycle_state='active' ORDER BY o.rowid LIMIT 1"
            ).fetchone()
            if operation:
                table, kind, row = "operations", "account", dict(operation)
            else:
                job = db.execute(
                    self._job_query()
                    + " WHERE j.state='queued' AND a.lifecycle_state='active' ORDER BY j.rowid LIMIT 1"
                ).fetchone()
                if job is None:
                    return None
                table, kind, row = "jobs", "upload", self._job_public(job)
            db.execute(f"UPDATE {table} SET state='running',code='',updated_at=? WHERE id=?", (_now(), row["id"]))
            self._active_id = row["id"]
            self._operation_stop = threading.Event()
            return kind, row

    def _run(self) -> None:
        try:
            while not self._shutdown.is_set():
                try:
                    claimed = self._claim()
                    if claimed is None:
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                    kind, row = claimed
                    self._execute(kind, row)
                    with self._active_guard:
                        self._active_id = None
                except Exception as exc:
                    with self._active_guard:
                        self._active_id = None
                        self._operation_stop.set()
                    if self._shutdown.is_set() or not self._recover_scheduler_failure(exc):
                        return
        finally:
            self._lock.release()
            self._release_lifetime_activity()

    def _recover_interrupted_records(self, *, timeout: float = 10) -> None:
        with self._db(timeout=timeout) as db:
            now = _now()
            db.execute("UPDATE jobs SET state='unknown',code='interrupted_result_unknown',updated_at=? WHERE state='running'", (now,))
            # Approval from a previous application run is not silently replayed.
            db.execute("UPDATE jobs SET state='draft',code='restart_confirmation_required',updated_at=? WHERE state='queued'", (now,))
            db.execute("UPDATE operations SET state='failed',code='operation_interrupted',updated_at=? WHERE state IN ('running','queued')", (now,))
            db.execute("UPDATE accounts SET auth_state='unchecked',code='operation_interrupted' WHERE auth_state='checking'")

    def _recover_scheduler_failure(self, exc: Exception) -> bool:
        with self._active_guard:
            self._scheduler_state = "recovering"
            self._scheduler_code = ("scheduler_database_unavailable"
                                    if isinstance(exc, sqlite3.Error) else "scheduler_failed")
        for delay in (0.0, 0.05, 0.15):
            if delay and self._shutdown.wait(delay):
                return False
            try:
                self._recover_interrupted_records(timeout=0.25)
            except Exception:
                continue
            with self._active_guard:
                self._scheduler_state = "running"
                self._scheduler_code = "scheduler_recovered"
            return True
        with self._active_guard:
            self._scheduler_state = "faulted"
        return False

    def _execute(self, kind: str, row: dict) -> None:
        monitor_done = threading.Event()
        monitor = threading.Thread(
            target=self._watch_cancellation, args=(kind, row["id"], monitor_done, self._operation_stop),
            name="open-flame-upload-cancel", daemon=True,
        )
        monitor.start()
        try:
            self._execute_operation(kind, row)
        finally:
            with self._active_guard:
                self._login_presentations.pop(row["id"], None)
            monitor_done.set()
            monitor.join(timeout=12)
            if kind == "account":
                with self._db() as db:
                    account = db.execute(
                        "SELECT lifecycle_state FROM accounts WHERE id=?", (row["account_id"],)
                    ).fetchone()
                if account is not None and account["lifecycle_state"] == "disconnected":
                    removed = self._remove_local_account_secret(row["platform"], row["account_id"])
                    with self._db() as db:
                        db.execute(
                            "UPDATE accounts SET code=? WHERE id=? AND lifecycle_state='disconnected'",
                            ("account_disconnected" if removed
                             else "account_disconnect_cleanup_failed", row["account_id"]),
                        )

    def _watch_cancellation(self, kind: str, operation_id: str, done: threading.Event, stop: threading.Event) -> None:
        table = "operations" if kind == "account" else "jobs"
        while not done.wait(0.2):
            if self._shutdown.is_set():
                stop.set()
                return
            try:
                with self._db() as db:
                    row = db.execute(f"SELECT code FROM {table} WHERE id=?", (operation_id,)).fetchone()
                if row and row["code"] == "cancellation_requested":
                    stop.set()
                    return
            except sqlite3.Error:
                # Unobservable cancellation must stop the owned operation too.
                stop.set()
                return

    def _execute_operation(self, kind: str, row: dict) -> None:
        invoked = False
        staged_paths: list[Path] = []
        try:
            if kind == "account":
                interactive = getattr(self.backend, "login_interactive", None)
                stop = self._operation_stop
                if row["action"] == "login" and callable(interactive):
                    self._record_login(row, stop, "preparing")
                    result = interactive(row["platform"], row["account_id"], stop,
                        lambda phase, png=None, expires_at=None: self._record_login(row, stop, phase, png, expires_at))
                else:
                    result = getattr(self.backend, row["action"])(row["platform"], row["account_id"], stop)
            else:
                with self._db() as db:
                    account = db.execute(
                        "SELECT auth_state,lifecycle_state FROM accounts WHERE id=?",
                        (row["account_id"],),
                    ).fetchone()
                if (account is None or account["lifecycle_state"] != "active"
                        or account["auth_state"] != "ready"):
                    raise UploadError("account_not_ready")
                validate_publish_schedule(
                    row["platform"], row["publish_at_unix"],
                    row["publish_timezone_offset_minutes"],
                    now=int(time.time()),
                )
                path = self._source_path(row["source_id"])
                with self._db() as db:
                    landscape, portrait = self._verified_job_cover_rows(
                        db,
                        row["platform"],
                        row["cover_landscape_asset_id"],
                        row["cover_portrait_asset_id"],
                    )
                landscape_path = None
                if landscape is not None:
                    _managed_path, payload = self._verified_asset_media(landscape)
                    landscape_path = self._stage_cover_payload(
                        row["id"], "landscape", landscape, payload
                    )
                    staged_paths.append(landscape_path)
                portrait_path = None
                if portrait is not None:
                    _managed_path, payload = self._verified_asset_media(portrait)
                    portrait_path = self._stage_cover_payload(
                        row["id"], "portrait", portrait, payload
                    )
                    staged_paths.append(portrait_path)
                if self._operation_stop.is_set():
                    result = BackendResult("canceled", "canceled_before_upload")
                else:
                    options = row["platform_options"]
                    request = UploadRequest(job_id=row["id"], account_id=row["account_id"], platform=row["platform"], file_path=path,
                                            title=row["title"], description=row["description"], tags=tuple(row["tags"]),
                                            category_id=row["category_id"], mode=row["mode"], copyright=row["copyright"], source_credit=row["source_credit"],
                                            cover_landscape_path=landscape_path,
                                            cover_portrait_path=portrait_path,
                                            publish_at_unix=row["publish_at_unix"],
                                            publish_timezone_offset_minutes=row["publish_timezone_offset_minutes"],
                                            dynamic=options.get("dynamic", ""),
                                            no_reprint=options.get("no_reprint", False),
                                            close_comments=options.get("close_comments", False),
                                            close_danmu=options.get("close_danmu", False),
                                            declaration=options.get("declaration"),
                                            short_title=options.get("short_title"),
                                            content_label=options.get("content_label"))
                    invoked = True
                    result = self.backend.upload(request, self._operation_stop)
        except UploadError as exc:
            result = BackendResult("unknown" if invoked else "failed", exc.code)
        except Exception:
            result = BackendResult("unknown" if invoked else "failed", "backend_result_unknown" if invoked else "backend_failed")
        finally:
            for path in staged_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if (not isinstance(result, BackendResult) or not isinstance(result.code, str)
            or not isinstance(result.status, str) or not _SAFE_CODE.fullmatch(result.code)):
            result = BackendResult("unknown" if invoked else "failed", "backend_result_invalid")
        now = _now()
        with self._db() as db:
            if kind == "account":
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM operations WHERE id=?", (row["id"],)).fetchone()
                account = db.execute("SELECT auth_state,lifecycle_state FROM accounts WHERE id=?",
                                     (row["account_id"],)).fetchone()
                if (current is None or current["state"] != "running"
                        or current["account_id"] != row["account_id"]
                        or account is None or account["lifecycle_state"] != "active"):
                    return
                if current["code"] == "cancellation_requested" or self._operation_stop.is_set():
                    result = BackendResult("canceled", "canceled")
                state = "ready" if result.status == "ready" else "canceled" if result.status in ("canceled", "cancelled") else "failed"
                auth_state = "ready" if state == "ready" else "unchecked" if state == "canceled" else "invalid"
                db.execute("UPDATE operations SET state=?,code=?,updated_at=? WHERE id=? AND state='running'", (state, result.code, now, row["id"]))
                db.execute("UPDATE accounts SET auth_state=?,code=? WHERE id=?", (auth_state, result.code, row["account_id"]))
            else:
                state = result.status
                if state not in ("submitted", "draft_saved", "failed", "unknown", "canceled"):
                    state = "unknown" if invoked else "failed"
                if invoked and state == "canceled":
                    state = "unknown"
                if state == "draft_saved" and row["mode"] != "draft":
                    state = "unknown"
                if state == "submitted" and row["mode"] != "publish":
                    state = "unknown"
                if result.code in _UPLOAD_AUTH_INVALID_CODES:
                    db.execute(
                        "UPDATE accounts SET auth_state='invalid',code=? "
                        "WHERE id=? AND lifecycle_state='active'",
                        (result.code, row["account_id"]),
                    )
                    db.execute(
                        "UPDATE jobs SET state='draft',"
                        "code='account_invalid_confirmation_revoked',updated_at=? "
                        "WHERE account_id=? AND state='queued'",
                        (now, row["account_id"]),
                    )
                db.execute("UPDATE jobs SET state=?,code=?,updated_at=? WHERE id=? AND state='running'", (state, result.code, now, row["id"]))
