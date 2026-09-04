"""Bounded, short-lived login presentation; never account state or raw output."""
from __future__ import annotations

import base64
import json
import stat
import struct
import time
import zlib
from pathlib import Path

PHASES = frozenset({"preparing", "waiting_scan", "scanned", "verification_required", "expired"})
MAX_QR_BYTES = 512 * 1024


def valid_png(payload: bytes) -> bool:
    if not isinstance(payload, bytes) or not 45 <= len(payload) <= MAX_QR_BYTES:
        return False
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    offset, first, image_data = 8, True, False
    while offset + 12 <= len(payload):
        size = int.from_bytes(payload[offset:offset + 4], "big")
        end = offset + 12 + size
        if end > len(payload):
            return False
        kind, content = payload[offset + 4:offset + 8], payload[offset + 8:end - 4]
        if zlib.crc32(kind + content) != int.from_bytes(payload[end - 4:end], "big"):
            return False
        if first:
            if kind != b"IHDR" or size != 13:
                return False
            width, height = struct.unpack(">II", content[:8])
            if not (80 <= width <= 1024 and 80 <= height <= 1024):
                return False
            first = False
        if kind == b"IDAT":
            image_data = True
        if kind == b"IEND":
            return size == 0 and end == len(payload) and image_data
        offset = end
    return False


def validate_update(phase, png=None, expires_at=None):
    if not isinstance(phase, str) or phase not in PHASES:
        raise ValueError("invalid_login_update")
    if expires_at is not None and (type(expires_at) is not int or not 0 < expires_at <= time.time() + 900):
        raise ValueError("invalid_login_update")
    if phase == "waiting_scan":
        if not valid_png(png):
            raise ValueError("invalid_login_update")
        if expires_at is not None and expires_at <= time.time():
            return "expired", None, expires_at
    elif png is not None:
        raise ValueError("invalid_login_update")
    return phase, png, expires_at


def update_writer(operation: Path):
    revision = 0

    def emit(phase, qr_png=None, expires_at=None):
        nonlocal revision
        phase, qr_png, expires_at = validate_update(phase, qr_png, expires_at)
        revision += 1
        data = {"revision": revision, "phase": phase, "expires_at": expires_at,
                "png": base64.b64encode(qr_png).decode("ascii") if qr_png else None}
        temporary = operation / "login-update.tmp"
        temporary.write_text(json.dumps(data), encoding="ascii")
        temporary.replace(operation / "login-update.json")
    return emit


def read_update(operation: Path, previous: int):
    path = operation / "login-update.json"
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 720 * 1024):
            return None
        payload = path.read_bytes()
        if len(payload) > 720 * 1024:
            return None
        data = json.loads(payload)
        if set(data) != {"revision", "phase", "png", "expires_at"}:
            return None
        if type(data["revision"]) is not int or not previous < data["revision"] < 10000:
            return None
        if data["png"] is not None and not isinstance(data["png"], str):
            return None
        png = base64.b64decode(data["png"], validate=True) if isinstance(data["png"], str) else None
        phase, png, expiry = validate_update(data["phase"], png, data["expires_at"])
        return data["revision"], phase, png, expiry
    except (OSError, ValueError, TypeError, KeyError):
        return None
