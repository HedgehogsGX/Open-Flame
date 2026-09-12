"""Shared parsing and result formatting for Worker command-line tools."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .adapters import YtDlpJsRuntime, YtDlpJsRuntimeName


def poll_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("poll interval must be a number") from exc
    if not math.isfinite(value) or not 0.1 <= value <= 60:
        raise argparse.ArgumentTypeError(
            "poll interval must be between 0.1 and 60 seconds"
        )
    return value


def js_runtime(raw: str) -> YtDlpJsRuntime:
    name, separator, raw_path = raw.partition(":")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use NAME:ABSOLUTE_EXECUTABLE"
        )
    try:
        return YtDlpJsRuntime(YtDlpJsRuntimeName(name), Path(raw_path))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "JavaScript runtime must use a supported name and absolute plain executable"
        ) from exc


def print_worker_result(result) -> None:
    if result is None:
        print(json.dumps({"status": "idle"}, ensure_ascii=False))
        return
    print(
        json.dumps(
            {
                "job_id": result.job_id,
                "attempt_id": result.attempt_id,
                "status": result.status,
                "error_code": result.error_code,
            },
            ensure_ascii=False,
        )
    )
