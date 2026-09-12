"""Upload cover bytes: bounded structural validation and complete image decoding.

File ownership, copying, domain transactions and platform slots remain callers'
responsibilities. Platform slot rules live in uploads.metadata.
"""
from __future__ import annotations

import struct
import zlib
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from .contracts import UploadError

COVER_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

MAX_COVER_BYTES = 20 * 1024**2
MAX_COVER_DECODED_BYTES = 64 * 1024**2
MAX_COVER_PIXELS = 40_000_000
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


def cover_metadata(payload: bytes, suffix: str) -> tuple[str, int, int]:
    parsers = {
        ".png": ("PNG", _png_dimensions),
        ".jpg": ("JPEG", _jpeg_structure_dimensions),
        ".jpeg": ("JPEG", _jpeg_structure_dimensions),
        ".webp": ("WEBP", _webp_structure_dimensions),
    }
    selected = parsers.get(suffix)
    structure = selected[1](payload) if selected is not None else None
    decoded = (
        _decoded_image_dimensions(payload, selected[0])
        if selected is not None and structure is not None
        else None
    )
    if selected is None or structure is None or decoded != structure:
        raise UploadError("invalid_cover_image")
    return COVER_MIME_TYPES[suffix], structure[0], structure[1]
