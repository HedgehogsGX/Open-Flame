from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from video_download_control.runtime_logging import (
    MAX_RUNTIME_LOG_LINE_BYTES,
    RUNTIME_LOG_SCHEMA_VERSION,
    RuntimeLogConfig,
    RuntimeLogConfigurationError,
    RuntimeLogger,
    read_recent_runtime_events,
)

NOW = datetime(2026, 9, 3, 12, 34, 56, 789000, tzinfo=UTC)
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")


def _config(
    tmp_path: Path,
    *,
    max_bytes: int = 1024 * 1024,
    backup_count: int = 5,
) -> RuntimeLogConfig:
    return RuntimeLogConfig(
        directory=(tmp_path / "logs").resolve(),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def _jsonl_records(path: Path) -> list[dict[str, object]]:
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    return [json.loads(line.decode("utf-8")) for line in payload.splitlines()]


def _all_log_records(directory: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("runtime-*.jsonl*")):
        records.extend(_jsonl_records(path))
    return records


def test_runtime_log_writes_one_bounded_versioned_json_object_per_line(
    tmp_path: Path,
) -> None:
    logger = RuntimeLogger(
        component="control",
        config=_config(tmp_path),
        clock=lambda: NOW,
        run_id="run-contract-1",
    )

    persisted = logger.emit(
        "control.started",
        app_version="0.7.2",
        app_schema_version=8,
    )

    assert persisted is True
    payload = logger.path.read_bytes()
    assert len(payload) <= MAX_RUNTIME_LOG_LINE_BYTES
    assert payload.count(b"\n") == 1
    record = _jsonl_records(logger.path)[0]
    assert record == {
        "app_schema_version": 8,
        "app_version": "0.7.2",
        "component": "control",
        "event": "control.started",
        "event_id": record["event_id"],
        "level": "INFO",
        "pid": record["pid"],
        "run_id": "run-contract-1",
        "schema_version": RUNTIME_LOG_SCHEMA_VERSION,
        "sequence": 1,
        "thread_id": record["thread_id"],
        "timestamp": "2026-09-03T12:34:56.789Z",
    }
    assert isinstance(record["pid"], int) and record["pid"] > 0
    assert isinstance(record["thread_id"], int) and record["thread_id"] > 0
    assert isinstance(record["event_id"], str)
    assert TOKEN_RE.fullmatch(record["event_id"])


def test_runtime_log_toolchain_event_has_a_narrow_path_free_contract(
    tmp_path: Path,
) -> None:
    path_sentinel = str((tmp_path / "SENTINEL-PRIVATE-TOOL-ROOT").resolve())
    url_sentinel = "https://secret.example.invalid/tool"
    logger = RuntimeLogger(
        component="control",
        config=_config(tmp_path),
        clock=lambda: NOW,
        run_id="run-toolchain-contract",
    )

    assert logger.emit(
        "toolchain.inspected",
        state="ready",
        detail_code="ok",
        yt_dlp_version="2026.08.19",
        ffmpeg_version="n9.0.1-6-g9d4ca21220-20260820",
        ffprobe_version="n9.0.1-6-g9d4ca21220-20260820",
        offline_smoke_passed=True,
    )
    assert logger.emit(
        "toolchain.inspected",
        state="unconfigured",
        detail_code="tool_root_unconfigured",
        offline_smoke_passed=False,
    )
    assert not logger.emit(
        "toolchain.inspected",
        state="invalid",
        detail_code="path_invalid",
        offline_smoke_passed=False,
        tool_root=path_sentinel,
    )
    assert not logger.emit(
        "toolchain.inspected",
        state="ready",
        detail_code="ok",
        yt_dlp_version=url_sentinel,
        offline_smoke_passed=True,
    )

    records = _jsonl_records(logger.path)
    inspected = [
        record for record in records if record["event"] == "toolchain.inspected"
    ]
    assert len(inspected) == 2
    base_fields = {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "component",
        "run_id",
        "event_id",
        "pid",
        "sequence",
        "thread_id",
    }
    assert set(inspected[0]) - base_fields == {
        "state",
        "detail_code",
        "yt_dlp_version",
        "ffmpeg_version",
        "ffprobe_version",
        "offline_smoke_passed",
    }
    assert set(inspected[1]) - base_fields == {
        "state",
        "detail_code",
        "offline_smoke_passed",
    }
    raw = logger.path.read_text(encoding="utf-8")
    assert path_sentinel not in raw
    assert url_sentinel not in raw
    assert logger.status()["rejected_events"] == 2


def test_runtime_log_reopen_appends_without_truncating_previous_run(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = RuntimeLogger(
        component="control", config=config, clock=lambda: NOW, run_id="run-first"
    )
    assert first.emit("control.started") is True

    second = RuntimeLogger(
        component="control", config=config, clock=lambda: NOW, run_id="run-second"
    )
    assert second.emit("control.stopped") is True

    records = _jsonl_records(second.path)
    assert [(record["event"], record["run_id"]) for record in records] == [
        ("control.started", "run-first"),
        ("control.stopped", "run-second"),
    ]


def test_runtime_log_rotates_at_size_boundary_and_keeps_valid_jsonl(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, max_bytes=1024, backup_count=3)
    logger = RuntimeLogger(
        component="control",
        config=config,
        clock=lambda: NOW,
        run_id="run-rotation",
    )

    for index in range(40):
        assert logger.emit(
            "http.request_completed",
            request_id=f"{index:032x}",
            method="GET",
            route="/health",
            status_code=200,
            duration_ms=float(index),
        )

    paths = sorted(config.directory.glob("runtime-control.jsonl*"))
    assert {path.name for path in paths} == {
        "runtime-control.jsonl",
        "runtime-control.jsonl.1",
        "runtime-control.jsonl.2",
        "runtime-control.jsonl.3",
    }
    assert all(0 < path.stat().st_size <= config.max_bytes for path in paths)
    retained = _all_log_records(config.directory)
    assert 0 < len(retained) < 40
    assert all(
        record["schema_version"] == RUNTIME_LOG_SCHEMA_VERSION for record in retained
    )
    assert len({record["event_id"] for record in retained}) == len(retained)
    assert len(logger.recent_events(limit=500)) == len(retained)


def test_runtime_log_concurrent_writers_do_not_interleave_json_lines(
    tmp_path: Path,
) -> None:
    logger = RuntimeLogger(
        component="control",
        config=_config(tmp_path),
        clock=lambda: NOW,
        run_id="run-concurrent",
    )

    def emit(index: int) -> bool:
        return logger.emit(
            "http.request_completed",
            request_id=f"{index:032x}",
            method="GET",
            route="/health",
            status_code=200,
            duration_ms=1.0,
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(emit, range(300)))

    records = _jsonl_records(logger.path)
    assert all(results)
    assert len(records) == 300
    assert {record["request_id"] for record in records} == {
        f"{index:032x}" for index in range(300)
    }
    assert len({record["event_id"] for record in records}) == 300


def test_runtime_log_write_failure_is_observable_and_does_not_escape(
    tmp_path: Path,
) -> None:
    sentinel = "SENTINEL-LOG-PATH-MUST-NOT-LEAK"
    blocked_directory = (tmp_path / sentinel).resolve()
    blocked_directory.write_text("not a directory", encoding="utf-8")
    logger = RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(directory=blocked_directory),
        run_id="run-write-failure",
    )

    initial = logger.status()
    persisted = logger.emit(
        "http.request_completed",
        request_id="1234567890abcdef1234567890abcdef",
        method="GET",
        route="/health",
        status_code=200,
        duration_ms=1.0,
    )
    failed = logger.status()

    assert initial == {
        "status": "error",
        "run_id": "run-write-failure",
        "write_failures": 0,
        "rejected_events": 0,
        "last_failure_code": "log_directory_unavailable",
    }
    assert persisted is False
    assert failed == {
        "status": "error",
        "run_id": "run-write-failure",
        "write_failures": 1,
        "rejected_events": 0,
        "last_failure_code": "log_write_failed",
    }
    assert sentinel not in json.dumps(failed)


def test_runtime_log_rejects_unknown_and_invalid_semantic_fields_without_leak(
    tmp_path: Path,
) -> None:
    sentinel = "SENTINEL_SECRET_VALUE_MUST_NOT_APPEAR"
    logger = RuntimeLogger(
        component="control",
        config=_config(tmp_path),
        clock=lambda: NOW,
        run_id="run-rejection",
    )

    assert logger.emit("control.started", secret_payload=sentinel) is False
    assert logger.emit("circuit.reset", platform=sentinel) is False
    assert logger.emit(f"invalid.{sentinel.lower()}") is False

    raw = logger.path.read_text(encoding="utf-8")
    records = _jsonl_records(logger.path)
    assert sentinel not in raw
    assert sentinel.lower() not in raw
    assert [record["event"] for record in records] == [
        "runtime_log.event_rejected",
        "runtime_log.event_rejected",
        "runtime_log.event_rejected",
    ]
    assert all(record["reason"] == "invalid_record" for record in records)
    assert logger.status()["status"] == "degraded"
    assert logger.status()["rejected_events"] == 3


def test_runtime_log_configuration_rejects_unsafe_bounds_and_identifiers(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeLogConfigurationError, match="absolute"):
        RuntimeLogConfig(directory=Path("relative/logs"))
    with pytest.raises(RuntimeLogConfigurationError, match="max bytes"):
        RuntimeLogConfig(directory=tmp_path.resolve(), max_bytes=1023)
    with pytest.raises(RuntimeLogConfigurationError, match="backup count"):
        RuntimeLogConfig(directory=tmp_path.resolve(), backup_count=21)
    with pytest.raises(RuntimeLogConfigurationError, match="component"):
        RuntimeLogger(component="../control", config=_config(tmp_path))
    with pytest.raises(RuntimeLogConfigurationError, match="instance"):
        RuntimeLogger(
            component="control",
            config=_config(tmp_path),
            instance_id="../other-process",
        )


def test_runtime_log_reader_rejects_tampered_or_semantically_invalid_records(
    tmp_path: Path,
) -> None:
    sentinel = "SENTINEL_TAMPERED_RECORD"
    logger = RuntimeLogger(
        component="control",
        config=_config(tmp_path),
        clock=lambda: NOW,
        run_id="run-reader",
    )
    assert logger.emit("control.started") is True
    valid = _jsonl_records(logger.path)[0]
    malformed_timestamp = {**valid, "timestamp": f"{sentinel}Z"}
    unknown_field = {**valid, "secret_payload": sentinel}
    invalid_semantic = {**valid, "event": "circuit.reset", "platform": sentinel}
    logger.path.write_text(
        "\n".join(
            json.dumps(record, separators=(",", ":"))
            for record in (valid, malformed_timestamp, unknown_field, invalid_semantic)
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    surfaced = read_recent_runtime_events(logger.config.directory, limit=100)

    assert surfaced == [valid]
    assert sentinel not in json.dumps(surfaced)
