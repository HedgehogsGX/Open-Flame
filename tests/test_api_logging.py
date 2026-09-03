from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import video_download_control.api as api_module
from video_download_control.api import create_app
from video_download_control.config import Settings
from video_download_control.runtime_logging import RuntimeLogConfig, RuntimeLogger
from video_download_control.toolchain import ToolchainStatus

REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _settings(tmp_path: Path) -> Settings:
    data_root = (tmp_path / "data").resolve()
    return Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        storage_min_free_bytes=0,
    )


def _logger(tmp_path: Path, *, run_id: str = "api-test-run") -> RuntimeLogger:
    return RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(directory=(tmp_path / "runtime-logs").resolve()),
        run_id=run_id,
    )


def _events(logger: RuntimeLogger, event: str) -> list[dict[str, object]]:
    return [item for item in logger.recent_events(limit=500) if item["event"] == event]


def test_api_lifecycle_records_exactly_one_start_and_stop(tmp_path: Path) -> None:
    logger = _logger(tmp_path)
    app = create_app(_settings(tmp_path), runtime_logger=logger)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    events = logger.recent_events(limit=500)
    assert [event["event"] for event in events].count("control.initializing") == 1
    assert [event["event"] for event in events].count("control.started") == 1
    assert [event["event"] for event in events].count("control.stopped") == 1
    assert {event["run_id"] for event in events} == {logger.run_id}


def test_startup_toolchain_inspection_is_cached_and_safely_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_marker = "SENTINEL-PRIVATE-TOOL-ROOT"
    query_marker = "SENTINEL-PRIVATE-TOOL-URL"
    data_root = (tmp_path / "data").resolve()
    tool_root = (tmp_path / private_marker).resolve()
    settings = Settings(
        data_root=data_root,
        database_path=data_root / "control.sqlite3",
        storage_min_free_bytes=0,
        tool_root=tool_root,
    )
    observed_roots: list[Path | None] = []

    def inspect(observed_root: Path | None) -> ToolchainStatus:
        observed_roots.append(observed_root)
        return ToolchainStatus(
            state="ready",
            detail_code="ok",
            yt_dlp_version="2026.08.19",
            ffmpeg_version="n9.0.1-6-g9d4ca21220-20260820",
            ffprobe_version="n9.0.1-6-g9d4ca21220-20260820",
            offline_smoke_passed=True,
            isolated_worker_ready=False,
            platform_download_verified=False,
            redistribution_status="local_private_only",
            network_download_enabled=False,
        )

    monkeypatch.setattr(api_module, "inspect_toolchain", inspect)
    logger = RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(
            directory=(tmp_path / "runtime-logs").resolve(),
            level="DEBUG",
        ),
        run_id="api-toolchain-cache-run",
    )
    app = create_app(settings, runtime_logger=logger)

    with TestClient(app) as client:
        first = client.get(
            "/api/v1/operations/tools",
            params={"source": f"https://secret.example.invalid/{query_marker}"},
        )
        second = client.get("/api/v1/operations/tools")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert observed_roots == [tool_root]
    inspected = _events(logger, "toolchain.inspected")
    assert len(inspected) == 1
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
    assert inspected[0]["state"] == "ready"
    assert inspected[0]["detail_code"] == "ok"
    assert inspected[0]["offline_smoke_passed"] is True
    tool_requests = [
        event
        for event in _events(logger, "http.request_completed")
        if event.get("route") == "/api/v1/operations/tools"
    ]
    assert len(tool_requests) == 2
    assert all(event["level"] == "DEBUG" for event in tool_requests)
    assert logger.status()["rejected_events"] == 0
    raw = logger.path.read_text(encoding="utf-8")
    assert private_marker not in raw
    assert query_marker not in raw


def test_api_generates_its_own_request_id_and_logs_the_route_template(
    tmp_path: Path,
) -> None:
    inbound_sentinel = "SENTINEL-INBOUND-REQUEST-ID-MUST-NOT-LEAK"
    logger = _logger(tmp_path)
    with TestClient(create_app(_settings(tmp_path), runtime_logger=logger)) as client:
        response = client.get(
            "/api/v1/batches/missing-batch-marker",
            headers={"X-Request-ID": inbound_sentinel},
        )

    request_id = response.headers["X-Request-ID"]
    records = _events(logger, "http.request_completed")
    matched = [record for record in records if record.get("request_id") == request_id]
    assert response.status_code == 404
    assert REQUEST_ID_RE.fullmatch(request_id)
    assert request_id != inbound_sentinel
    assert len(matched) == 1
    assert matched[0]["level"] == "WARNING"
    assert matched[0]["method"] == "GET"
    assert matched[0]["route"] == "/api/v1/batches/{batch_id}"
    assert matched[0]["status_code"] == 404
    raw = logger.path.read_text(encoding="utf-8")
    assert inbound_sentinel not in raw
    assert "missing-batch-marker" not in raw


def test_api_logs_404_and_422_without_paths_or_request_bodies(
    tmp_path: Path,
) -> None:
    path_sentinel = "SENTINEL-UNMATCHED-PATH"
    body_sentinel = "SENTINEL-BODY-NAME"
    logger = _logger(tmp_path)
    with TestClient(create_app(_settings(tmp_path), runtime_logger=logger)) as client:
        missing = client.get(f"/{path_sentinel}")
        invalid = client.post(
            "/api/v1/batches",
            json={"name": body_sentinel, "inputs": []},
        )

    records = _events(logger, "http.request_completed")
    by_request = {record["request_id"]: record for record in records}
    missing_record = by_request[missing.headers["X-Request-ID"]]
    invalid_record = by_request[invalid.headers["X-Request-ID"]]
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert (missing_record["route"], missing_record["status_code"]) == (
        "/unmatched",
        404,
    )
    assert (invalid_record["route"], invalid_record["status_code"]) == (
        "/api/v1/batches",
        422,
    )
    assert missing_record["level"] == invalid_record["level"] == "WARNING"
    raw = logger.path.read_text(encoding="utf-8")
    assert path_sentinel not in raw
    assert body_sentinel not in raw


def test_log_endpoint_does_not_expose_query_body_names_urls_or_log_paths(
    tmp_path: Path,
) -> None:
    query_sentinel = "SENTINEL_QUERY_NAME"
    body_name_sentinel = "SENTINEL_BODY_BATCH_NAME"
    body_url_sentinel = "sentinel-body-url-marker"
    import_name_sentinel = "SENTINEL_IMPORT_NAME"
    filename_sentinel = "SENTINEL_IMPORT_FILENAME.csv"
    log_path_sentinel = "SENTINEL-PRIVATE-LOG-DIRECTORY"
    settings = _settings(tmp_path)
    logger = RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(
            directory=(tmp_path / log_path_sentinel / "logs").resolve()
        ),
        run_id="api-private-path-run",
    )
    app = create_app(settings, runtime_logger=logger)

    with TestClient(app) as client:
        created = client.post(
            f"/api/v1/batches?diagnostic_name={quote(query_sentinel)}",
            json={
                "name": body_name_sentinel,
                "inputs": [f"https://youtu.be/{body_url_sentinel}"],
            },
        )
        imported = client.post(
            "/api/v1/batches/import"
            f"?filename={quote(filename_sentinel)}&name={quote(import_name_sentinel)}",
            content=b"https://youtu.be/import-body-marker\n",
            headers={"content-type": "text/plain"},
        )
        logs = client.get(
            f"/api/v1/operations/logs?limit=500&name={quote(query_sentinel)}"
        )

    assert created.status_code == 201
    assert imported.status_code == 201
    assert logs.status_code == 200
    payload = logs.json()
    assert payload["status"] == "ok"
    assert payload["run_id"] == logger.run_id
    assert 1 <= len(payload["events"]) <= 500
    serialized_endpoint = json.dumps(payload, ensure_ascii=False)
    raw_log = "".join(
        path.read_text(encoding="utf-8")
        for path in logger.config.directory.glob("runtime-*.jsonl*")
    )
    for sentinel in (
        query_sentinel,
        body_name_sentinel,
        body_url_sentinel,
        import_name_sentinel,
        filename_sentinel,
        "import-body-marker",
        log_path_sentinel,
    ):
        assert sentinel not in serialized_endpoint
        assert sentinel not in raw_log


def test_log_endpoint_is_bounded_and_reports_logging_failure_without_path_leak(
    tmp_path: Path,
) -> None:
    path_sentinel = "SENTINEL-UNWRITABLE-LOG-PATH"
    blocked = (tmp_path / path_sentinel).resolve()
    blocked.write_text("not a directory", encoding="utf-8")
    logger = RuntimeLogger(
        component="control",
        config=RuntimeLogConfig(directory=blocked),
        run_id="api-log-failure-run",
    )

    with TestClient(create_app(_settings(tmp_path), runtime_logger=logger)) as client:
        response = client.get("/api/v1/operations/logs?limit=500")
        too_small = client.get("/api/v1/operations/logs?limit=0")
        too_large = client.get("/api/v1/operations/logs?limit=501")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "error"
    assert payload["write_failures"] >= 2
    assert payload["last_failure_code"] == "log_write_failed"
    assert payload["events"] == []
    assert path_sentinel not in response.text
    assert too_small.status_code == 422
    assert too_large.status_code == 422
    assert REQUEST_ID_RE.fullmatch(too_small.headers["X-Request-ID"])
    assert REQUEST_ID_RE.fullmatch(too_large.headers["X-Request-ID"])


def test_concurrent_api_requests_receive_unique_correlated_request_ids(
    tmp_path: Path,
) -> None:
    logger = _logger(tmp_path, run_id="api-concurrent-run")
    app = create_app(_settings(tmp_path), runtime_logger=logger)

    with TestClient(app) as client:

        def fetch(_: int) -> tuple[int, str]:
            response = client.get("/health")
            return response.status_code, response.headers["X-Request-ID"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(fetch, range(64)))

    request_ids = {request_id for status, request_id in responses if status == 200}
    logged = {
        str(record["request_id"])
        for record in _events(logger, "http.request_completed")
        if record.get("route") == "/health"
    }
    assert len(responses) == 64
    assert all(status == 200 for status, _ in responses)
    assert len(request_ids) == 64
    assert all(REQUEST_ID_RE.fullmatch(request_id) for request_id in request_ids)
    assert logged == request_ids
