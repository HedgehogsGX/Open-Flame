from __future__ import annotations

import csv
import io
from pathlib import Path

from .normalization import extract_urls


MAX_IMPORT_BYTES = 256 * 1024
MAX_IMPORT_CELL_LENGTH = 8192
URL_HEADERS = frozenset(
    {
        "url",
        "link",
        "source_url",
        "video_url",
        "网址",
        "链接",
        "视频链接",
    }
)


class BatchImportError(ValueError):
    pass


def parse_batch_file(
    content: bytes,
    *,
    filename: str,
    max_bytes: int = MAX_IMPORT_BYTES,
) -> list[str]:
    if not filename or len(filename) > 255 or "\x00" in filename:
        raise BatchImportError("导入文件名无效")
    if len(content) > max_bytes:
        raise BatchImportError(f"导入文件不得超过 {max_bytes} bytes")
    if b"\x00" in content:
        raise BatchImportError("导入文件不得包含 NUL 字节")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BatchImportError("导入文件必须使用 UTF-8 编码") from exc
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        values = [line for line in text.splitlines() if line.strip()]
    elif suffix == ".csv":
        values = _parse_csv(text)
    else:
        raise BatchImportError("仅支持 .txt 或 .csv 文件")
    if not values:
        raise BatchImportError("导入文件没有可处理的非空记录")
    return values


def _parse_csv(text: str) -> list[str]:
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise BatchImportError("CSV 格式无效") from exc
    if not rows:
        return []
    if any(len(cell) > MAX_IMPORT_CELL_LENGTH for row in rows for cell in row):
        raise BatchImportError("CSV 单元格过长")

    header = [cell.strip().lower() for cell in rows[0]]
    url_indexes = [index for index, cell in enumerate(header) if cell in URL_HEADERS]
    if len(url_indexes) > 1:
        raise BatchImportError("CSV 只能包含一个明确的 URL 列")
    if url_indexes:
        index = url_indexes[0]
        return [
            row[index]
            for row in rows[1:]
            if index < len(row) and row[index].strip()
        ]

    values: list[str] = []
    for row in rows:
        discovered = [url for cell in row for url in extract_urls(cell)]
        if discovered:
            values.extend(discovered)
        elif any(cell.strip() for cell in row):
            values.append(" ".join(cell for cell in row if cell.strip()))
    return values
