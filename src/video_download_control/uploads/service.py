"""Durable, explicit upload submission, separate from download recovery.

An interrupted upload may already exist on the platform. Such an operation is
never retried automatically. The immutable local draft remains reviewable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

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
    normalize_tencent_short_title,
)
from .login_progress import validate_update
from .schema import UploadSchemaError, ensure_upload_schema

MAX_SOURCE_BYTES = 2 * 1024**3
MAX_COVER_BYTES = 20 * 1024**2
MAX_COVER_DECODED_BYTES = 64 * 1024**2
MAX_COVER_PIXELS = 40_000_000
UPLOAD_RESERVE_BYTES = 64 * 1024**2
TITLE_LIMITS = {"bilibili": 80, "douyin": 30, "tencent": 100}
# Reserve the platform's minimum lead plus the real backend's two-hour upload
# timeout and five minutes for process/form overhead.  Browser adapters perform
# a separate final-action check against the platform minimum itself.
SCHEDULE_LEAD_SECONDS = {
    "bilibili": 6 * 60 * 60 + 5 * 60,
    "douyin": 4 * 60 * 60 + 5 * 60,
    "tencent": 4 * 60 * 60 + 5 * 60,
}
DOUYIN_DECLARATIONS = frozenset({
    "内容由AI生成", "内容为转载信息", "内容为个人观点或见解",
})
TENCENT_CONTENT_LABELS = frozenset({"含AI生成内容"})
TENCENT_SCHEDULE_MAX_SECONDS = 28 * 24 * 60 * 60
_PLATFORM_OPTION_KEYS = {
    "bilibili": frozenset({"dynamic", "no_reprint", "close_comments", "close_danmu"}),
    "douyin": frozenset({"declaration"}),
    "tencent": frozenset({"short_title", "content_label"}),
}
_TARGET_OVERRIDE_KEYS = frozenset({
    "account_id", "title", "description", "tags", "category_id", "mode",
    "copyright", "source_credit", "cover_landscape_asset_id",
    "cover_portrait_asset_id", "publish_at_unix",
    "publish_timezone_offset_minutes", "platform_options",
})
_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
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
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & reparse
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)):
        raise UploadError("unsafe_upload_file")
    return info


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise UploadError("invalid_identifier")
    return value


def _text(value: str, maximum: int, *, required: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\t\r" for c in value)):
        raise UploadError("invalid_metadata")
    if required and not value.strip():
        raise UploadError("invalid_metadata")
    return value.strip()


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
        name = _text(name, 60, required=True)
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
        if cached is not None and cached[0] == _signature(info) and not cached[1]:
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
            with path.open("rb") as handle:
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("source_changed")
            after = _plain(path)
            valid = digest == row["sha256"] and _signature(after) == _signature(before)
            self._source_integrity_cache[row["id"]] = (_signature(after), valid)
            if not valid:
                raise UploadError("source_changed")
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
        if cached is not None and cached[0] == _signature(info) and not cached[1]:
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
            with path.open("rb") as handle:
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("cover_changed")
                payload = handle.read(MAX_COVER_BYTES + 1)
                if _signature(os.fstat(handle.fileno())) != _signature(before):
                    raise UploadError("cover_changed")
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
                and _signature(after) == _signature(before)
            )
            self._asset_integrity_cache[row["id"]] = (_signature(after), valid)
            if not valid:
                raise UploadError("cover_changed")
        except FileNotFoundError:
            raise UploadError("cover_reimport_required") from None
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
    def import_cover(self, path: Path, name: str) -> dict:
        name = _text(name, 180, required=True)
        if any(char in name for char in '/\\:\x00') or name in (".", ".."):
            raise UploadError("invalid_cover_name")
        suffix = Path(name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise UploadError("unsupported_cover_type")
        try:
            before = _plain(path)
        except OSError:
            raise UploadError("cover_unavailable") from None
        if not 0 < before.st_size <= MAX_COVER_BYTES:
            raise UploadError("cover_size_invalid")
        if shutil.disk_usage(self.root).free < before.st_size + UPLOAD_RESERVE_BYTES:
            raise UploadError("upload_storage_full")
        asset_id = uuid4().hex
        target = self.root / "assets" / f"{asset_id}{suffix}"
        _plain(target.parent, directory=True)
        digest, total = hashlib.sha256(), 0
        try:
            with path.open("rb") as src, target.open("xb") as dst:
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("cover_changed")
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_COVER_BYTES:
                        raise UploadError("cover_size_invalid")
                    dst.write(chunk)
                    digest.update(chunk)
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("cover_changed")
                dst.flush()
                os.fsync(dst.fileno())
            if total != before.st_size or _signature(_plain(path)) != _signature(before):
                raise UploadError("cover_changed")
            payload = target.read_bytes()
            mime_type, width, height = _cover_metadata(payload, suffix)
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO upload_assets(id,kind,name,suffix,mime_type,size,sha256,width,height,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (asset_id, "cover", name, suffix, mime_type, total, digest.hexdigest(),
                     width, height, _now()),
                )
                row = db.execute(self._asset_query() + " WHERE u.id=?", (asset_id,)).fetchone()
                return self._asset_public(row)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

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
                            if _signature(_plain(target)) != _signature(before):
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
    def import_source(self, path: Path, name: str, expected_sha256: str | None = None) -> dict:
        name = _text(name, 180, required=True)
        if any(char in name for char in '/\\:\x00') or name in (".", ".."):
            raise UploadError("invalid_source_name")
        suffix = Path(name).suffix.lower()
        if suffix not in (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"):
            raise UploadError("unsupported_video_type")
        try:
            before = _plain(path)
        except OSError:
            raise UploadError("source_unavailable") from None
        if not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise UploadError("source_size_invalid")
        if shutil.disk_usage(self.root).free < before.st_size + UPLOAD_RESERVE_BYTES:
            raise UploadError("upload_storage_full")
        source_id = uuid4().hex
        target = self.root / "media" / f"{source_id}{suffix}"
        _plain(target.parent, directory=True)
        digest, total = hashlib.sha256(), 0
        try:
            with path.open("rb") as src, target.open("xb") as dst:
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SOURCE_BYTES:
                        raise UploadError("source_size_invalid")
                    dst.write(chunk)
                    digest.update(chunk)
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                dst.flush()
                os.fsync(dst.fileno())
            if total != before.st_size or _signature(_plain(path)) != _signature(before):
                raise UploadError("source_changed")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise UploadError("source_hash_mismatch")
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO sources(id,name,suffix,size,sha256,created_at) VALUES(?,?,?,?,?,?)",
                    (source_id, name, suffix, total, digest.hexdigest(), _now()),
                )
                row = db.execute(self._source_query() + " WHERE s.id=?", (source_id,)).fetchone()
                return self._source_public(row)
        except BaseException:
            if target.exists():
                target.unlink()
            raise

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
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    digest.update(chunk)
                    dst.write(chunk)
                if _signature(os.fstat(src.fileno())) != _signature(before):
                    raise UploadError("source_changed")
                dst.flush()
                os.fsync(dst.fileno())
            if (total != row["size"] or digest.hexdigest() != row["sha256"]
                    or _signature(_plain(path)) != _signature(before)):
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

    def _get_job(self, db, job_id: str) -> dict:
        row = db.execute(self._job_query() + " WHERE j.id=?", (_identifier(job_id),)).fetchone()
        if row is None:
            raise UploadError("job_not_found")
        return self._job_public(row)

    @staticmethod
    def _normalize_tags(tags) -> list[str]:
        if not isinstance(tags, list) or len(tags) > 10:
            raise UploadError("invalid_tags")
        normalized = [_text(tag, 20, required=True) for tag in tags]
        if (len(set(normalized)) != len(normalized)
                or any(any(c in tag for c in ",，#＃\n\r\t") for tag in normalized)):
            raise UploadError("invalid_tags")
        return normalized

    @staticmethod
    def _normalize_v2_replay_tags(tags) -> list[str]:
        """Map only a pre-Schema-3 request onto its migrated stored tags."""
        if not isinstance(tags, list) or len(tags) > 10:
            raise UploadError("invalid_tags")
        legacy = [_text(tag, 20, required=True) for tag in tags]
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

    @staticmethod
    def _normalize_platform_options(platform: str, value) -> dict:
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - _PLATFORM_OPTION_KEYS[platform]:
            raise UploadError("unsupported_platform_option")
        if platform == "bilibili":
            dynamic_value = value.get("dynamic", "")
            dynamic = "" if dynamic_value is None else _text(dynamic_value, 250)
            options = {"dynamic": dynamic}
            for key in ("no_reprint", "close_comments", "close_danmu"):
                option = value.get(key, False)
                if type(option) is not bool:
                    raise UploadError("invalid_platform_option")
                options[key] = option
        elif platform == "douyin":
            declaration = value.get("declaration")
            if declaration is not None:
                declaration = _text(declaration, 30, required=True)
                if declaration not in DOUYIN_DECLARATIONS:
                    raise UploadError("unsupported_declaration")
            options = {"declaration": declaration}
        else:
            short_title = value.get("short_title")
            if short_title is not None:
                short_title = _text(short_title, 15, required=True)
                if len(short_title) < 7:
                    raise UploadError("tencent_short_title_length")
            content_label = value.get("content_label")
            if content_label is not None:
                content_label = _text(content_label, 30, required=True)
                if content_label not in TENCENT_CONTENT_LABELS:
                    raise UploadError("unsupported_content_label")
            options = {"short_title": short_title, "content_label": content_label}
        if len(json.dumps(options, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True).encode("utf-8")) > 4096:
            raise UploadError("platform_options_too_large")
        return options

    @staticmethod
    def _validate_schedule(platform: str, publish_at_unix, offset_minutes,
                           *, now: int | None = None) -> tuple[int | None, int | None]:
        if publish_at_unix is None:
            if offset_minutes is not None:
                raise UploadError("publish_timezone_without_time")
            return None, None
        if (type(publish_at_unix) is not int or not 1_700_000_000 <= publish_at_unix <= 4_102_444_800
                or type(offset_minutes) is not int or not -840 <= offset_minutes <= 840):
            raise UploadError("invalid_publish_time")
        current = int(time.time()) if now is None else now
        if publish_at_unix <= current + SCHEDULE_LEAD_SECONDS[platform]:
            raise UploadError("publish_time_too_soon")
        if publish_at_unix % 60:
            raise UploadError("publish_time_precision_unsupported")
        if platform == "tencent":
            if (publish_at_unix + offset_minutes * 60) % 3600:
                raise UploadError("tencent_schedule_requires_whole_hour")
            if now is None and publish_at_unix > current + TENCENT_SCHEDULE_MAX_SECONDS:
                raise UploadError("tencent_schedule_too_far")
        return publish_at_unix, offset_minutes

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
                          verify_assets: bool = True, enforce_schedule: bool = True) -> dict:
        platform = account["platform"]
        if platform != "bilibili" and any(
            key in override for key in ("category_id", "copyright", "source_credit")
        ):
            raise UploadError("unsupported_platform_field")
        raw = {**base, **{key: value for key, value in override.items() if key != "account_id"}}
        title = _text(raw["title"], 100, required=True)
        if len(title) > TITLE_LIMITS[platform]:
            raise UploadError("title_too_long")
        description = _text(raw.get("description", ""), 2000)
        tags = self._normalize_tags(raw.get("tags", []))
        mode = raw.get("mode", "publish")
        if mode not in {"publish", "draft"}:
            raise UploadError("invalid_metadata")
        if mode == "draft" and platform != "tencent":
            raise UploadError("draft_mode_unsupported")

        category_id = raw.get("category_id")
        copyright = raw.get("copyright")
        source_credit = _text(raw.get("source_credit", ""), 200)
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

        publish_at, offset = self._validate_schedule(
            platform, raw.get("publish_at_unix"), raw.get("publish_timezone_offset_minutes"),
            now=None if enforce_schedule else 0,
        )
        if mode == "draft" and publish_at is not None:
            raise UploadError("draft_schedule_unsupported")
        options = self._normalize_platform_options(platform, raw.get("platform_options"))
        if platform == "tencent":
            options["short_title"] = normalize_tencent_short_title(
                options["short_title"] if options["short_title"] is not None else title
            )
        return {
            "account_id": account["id"], "platform": platform, "title": title,
            "description": description, "tags": tags, "category_id": category_id,
            "mode": mode, "copyright": copyright, "source_credit": source_credit,
            "cover_landscape_asset_id": landscape_id,
            "cover_portrait_asset_id": portrait_id, "publish_at_unix": publish_at,
            "publish_timezone_offset_minutes": offset, "platform_options": options,
        }

    @_requires_activity
    def create_jobs(self, *, source_id: str, account_ids: list[str], title: str,
                    description: str, tags: list[str], idempotency_key: str,
                    category_id: int | None = None, mode: str = "publish",
                    copyright: int | None = None, source_credit: str = "",
                    target_overrides: list[dict] | None = None) -> list[dict]:
        _identifier(source_id)
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 20 or len(set(account_ids)) != len(account_ids):
            raise UploadError("invalid_accounts")
        for account_id in account_ids:
            _identifier(account_id)
        title = _text(title, 100, required=True)
        description = _text(description, 2000)
        source_credit = _text(source_credit, 200)
        raw_tags = tags
        try:
            tags = self._normalize_tags(tags)
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
            if (not isinstance(override, dict) or set(override) - _TARGET_OVERRIDE_KEYS
                    or set(override) == {"account_id"}):
                raise UploadError("invalid_target_override")
            account_id = _identifier(override.get("account_id"))
            if account_id not in account_ids or account_id in override_map:
                raise UploadError("invalid_target_override")
            override_map[account_id] = override
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM requests WHERE id=?", (idempotency_key,)).fetchone()
            accounts_by_id = {}
            for account_id in account_ids:
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise UploadError("account_not_found")
                accounts_by_id[account_id] = account
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
                return [self._get_job(db, job_id) for job_id in json.loads(prior["job_ids"])]
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
            if job["state"] != "draft":
                raise UploadError("job_requires_new_draft")
            if not self.backend.inspect().get("ready"):
                raise UploadError("runtime_missing")
            account = db.execute(
                "SELECT auth_state,lifecycle_state FROM accounts WHERE id=?",
                (job["account_id"],),
            ).fetchone()
            if account["lifecycle_state"] != "active":
                raise UploadError("account_disconnected")
            if account["auth_state"] != "ready":
                raise UploadError("account_not_ready")
            self._validate_schedule(
                job["platform"], job["publish_at_unix"],
                job["publish_timezone_offset_minutes"],
            )
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
            db.execute("UPDATE jobs SET state='queued',code='',updated_at=? WHERE id=?", (_now(), job_id))
            result = self._get_job(db, job_id)
        self._wake.set()
        return result

    @_requires_activity
    def cancel(self, job_id: str) -> dict:
        with self._active_guard, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get_job(db, job_id)
            if job["state"] in ("draft", "queued"):
                db.execute("UPDATE jobs SET state='canceled',code='canceled',updated_at=? WHERE id=?", (_now(), job_id))
            elif job["state"] == "running":
                db.execute("UPDATE jobs SET code='cancellation_requested',updated_at=? WHERE id=?", (_now(), job_id))
                if self._active_id == job_id:
                    self._operation_stop.set()
            return self._get_job(db, job_id)

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
            self._validate_schedule(
                job["platform"], job["publish_at_unix"],
                job["publish_timezone_offset_minutes"],
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
                self._validate_schedule(
                    row["platform"], row["publish_at_unix"],
                    row["publish_timezone_offset_minutes"],
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
                db.execute("UPDATE jobs SET state=?,code=?,updated_at=? WHERE id=? AND state='running'", (state, result.code, now, row["id"]))
