from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest

import video_download_control.capability_evidence as capability_evidence_module
from video_download_control.build_identity import (
    ProductBuildDriftError,
    current_product_identity,
)
from video_download_control.capability_cli import main
from video_download_control.database import (
    MIGRATION_2_SQL,
    MIGRATION_3_SQL,
    MIGRATION_4_SQL,
    MIGRATION_5_SQL,
    MIGRATION_6_SQL,
    MIGRATION_7_SQL,
    SCHEMA_V1_SQL,
    Database,
)


ENVIRONMENT = "windows-x64-direct-no-cookie"
MANIFEST_FIELDS = (
    "sample_id",
    "platform",
    "source_type",
    "job_kind",
    "url",
    "expected_outcome",
    "expected_output_count",
    "requires_cookie",
    "region",
)
RESULT_FIELDS = (
    "run_id",
    "sample_id",
    "job_kind",
    "observed_outcome",
    "observed_output_count",
    "completed_at",
    "adapter",
    "downloader_version",
    "environment",
    "product_version",
)


def _csv_text(
    fields: tuple[str, ...], rows: list[dict[str, object]]
) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_stage0_bundle(
    root: Path,
    *,
    product_version: str | None = None,
    positive_count: int = 10,
) -> tuple[Path, Path, tuple[str, ...]]:
    resolved_product_version = (
        current_product_identity() if product_version is None else product_version
    )
    sample_canaries = tuple(
        f"SAMPLE-CANARY-positive-{index}" for index in range(positive_count)
    )
    samples = [
        {
            "sample_id": sample_id,
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": f"https://www.youtube.com/watch?v=RAWURLCANARY{index}",
            "expected_outcome": "ready",
            "expected_output_count": 1,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
        for index, sample_id in enumerate(sample_canaries)
    ]
    negative_sample = "SAMPLE-CANARY-negative"
    samples.append(
        {
            "sample_id": negative_sample,
            "platform": "youtube",
            "source_type": "youtube_video",
            "job_kind": "download",
            "url": "https://www.youtube.com/watch?v=RAWURLCANARY-unavailable",
            "expected_outcome": "content_unavailable",
            "expected_output_count": 0,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
    )
    results: list[dict[str, object]] = []
    for run in range(1, 4):
        for sample in samples:
            results.append(
                {
                    "run_id": f"RUN-CANARY-{run}",
                    "sample_id": sample["sample_id"],
                    "job_kind": "download",
                    "observed_outcome": sample["expected_outcome"],
                    "observed_output_count": sample["expected_output_count"],
                    "completed_at": f"2026-09-0{run}T10:00:00+09:30",
                    "adapter": "yt_dlp",
                    "downloader_version": "2026.08.19",
                    "environment": ENVIRONMENT,
                    "product_version": resolved_product_version,
                }
            )

    manifest = root / "PATH-CANARY-manifest.csv"
    result_path = root / "PATH-CANARY-results.csv"
    manifest.write_text(
        _csv_text(MANIFEST_FIELDS, samples),
        encoding="utf-8",
        newline="",
    )
    result_path.write_text(
        _csv_text(RESULT_FIELDS, results),
        encoding="utf-8",
        newline="",
    )
    return manifest, result_path, (*sample_canaries, negative_sample)


def _initialized_database(path: Path) -> Path:
    Database(path).initialize()
    return path


def _schema_nine_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
        )
        connection.commit()
        for migration in (
            MIGRATION_2_SQL,
            MIGRATION_3_SQL,
            MIGRATION_4_SQL,
            MIGRATION_5_SQL,
            MIGRATION_6_SQL,
            MIGRATION_7_SQL,
        ):
            connection.executescript(migration)
    finally:
        connection.close()
    database = Database(path)
    database._migrate_to_8()
    database._migrate_to_9()
    return path


def _invoke(capsys, argv: list[str]) -> tuple[int, str, str]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _assert_private_canaries_absent(
    output: str,
    *,
    manifest: Path,
    results: Path,
    sample_canaries: tuple[str, ...],
) -> None:
    forbidden = (
        "RAWURLCANARY",
        "RUN-CANARY",
        "PATH-CANARY",
        manifest.name,
        results.name,
        str(manifest),
        str(results),
        *sample_canaries,
    )
    for canary in forbidden:
        assert canary not in output


def test_cli_import_list_approve_and_history_do_not_expose_private_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    manifest, results, sample_canaries = _write_stage0_bundle(tmp_path)
    base = ["--database-path", str(database_path)]
    outputs: list[str] = []

    code, stdout, stderr = _invoke(
        capsys,
        [
            *base,
            "import",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
            "--environment-key",
            ENVIRONMENT,
        ],
    )
    assert code == 0
    assert stderr == ""
    imported = json.loads(stdout)
    assert imported["operation"] == "import"
    assert imported["created"] is True
    assert imported["created_count"] == 1
    evidence_id = imported["evidence_ids"][0]
    outputs.append(stdout)

    code, stdout, stderr = _invoke(
        capsys,
        [*base, "list", "--kind", "evidence"],
    )
    assert code == 0
    assert stderr == ""
    evidence_listing = json.loads(stdout)
    assert evidence_listing["records"][0]["assessment"] == "qualified"
    identity_key = evidence_listing["records"][0]["identity_key"]
    outputs.append(stdout)

    code, stdout, stderr = _invoke(
        capsys,
        [
            *base,
            "approve",
            "--evidence-id",
            evidence_id,
            "--expected-revision",
            "0",
            "--reason-code",
            "stage0-reviewed",
        ],
    )
    assert code == 0
    assert stderr == ""
    approved = json.loads(stdout)
    assert approved["decision"]["state"] == "approved"
    assert approved["decision"]["revision"] == 1
    outputs.append(stdout)

    code, stdout, stderr = _invoke(
        capsys,
        [*base, "history", "--identity-key", identity_key],
    )
    assert code == 0
    assert stderr == ""
    history = json.loads(stdout)
    assert history["count"] == 1
    assert history["records"][0]["state"] == "approved"
    outputs.append(stdout)

    _assert_private_canaries_absent(
        "".join(outputs),
        manifest=manifest,
        results=results,
        sample_canaries=sample_canaries,
    )


def test_cli_validation_error_is_stable_json_without_url_path_sample_or_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    manifest, results, sample_canaries = _write_stage0_bundle(
        tmp_path,
        product_version="0.12.0",
    )

    code, stdout, stderr = _invoke(
        capsys,
        [
            "--database-path",
            str(database_path),
            "import",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
            "--environment-key",
            ENVIRONMENT,
        ],
    )

    assert code == 3
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "product_version_mismatch",
    }
    _assert_private_canaries_absent(
        stderr,
        manifest=manifest,
        results=results,
        sample_canaries=sample_canaries,
    )


def test_cli_missing_private_input_does_not_echo_its_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    missing_manifest = (tmp_path / "PATH-CANARY-missing-manifest.csv").resolve()
    missing_results = (tmp_path / "PATH-CANARY-missing-results.csv").resolve()

    code, stdout, stderr = _invoke(
        capsys,
        [
            "--database-path",
            str(database_path),
            "import",
            "--manifest",
            str(missing_manifest),
            "--results",
            str(missing_results),
            "--environment-key",
            ENVIRONMENT,
        ],
    )

    assert code == 6
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "input_unavailable",
    }
    assert "PATH-CANARY" not in stderr
    assert str(missing_manifest) not in stderr
    assert str(missing_results) not in stderr


def test_cli_rejects_hard_linked_database_without_opening_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    alias = tmp_path / "database-hardlink.sqlite3"
    try:
        os.link(database_path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {type(exc).__name__}")
    before = database_path.read_bytes()

    code, stdout, stderr = _invoke(
        capsys,
        ["--database-path", str(alias), "list", "--kind", "evidence"],
    )

    assert code == 2
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "hard_link_rejected",
    }
    assert database_path.read_bytes() == before


def test_cli_sanitizes_invalid_argument_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stdout, stderr = _invoke(
        capsys,
        ["--PRIVATE-OPTION-CANARY", r"C:\Users\PRIVATE-PATH-CANARY"],
    )

    assert code == 2
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "invalid_arguments",
    }
    assert "PRIVATE" not in stderr


def test_cli_sanitizes_product_build_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    manifest, results, _ = _write_stage0_bundle(tmp_path)

    def fail_identity() -> str:
        raise ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY")

    monkeypatch.setattr(
        capability_evidence_module, "current_product_identity", fail_identity
    )
    code, stdout, stderr = _invoke(
        capsys,
        [
            "--database-path",
            str(database_path),
            "import",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
            "--environment-key",
            ENVIRONMENT,
        ],
    )

    assert code == 6
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "product_build_drift",
    }
    assert "PRIVATE" not in stderr


@pytest.mark.parametrize(
    "command",
    (
        ["list", "--kind", "evidence"],
        ["history", "--identity-key", "0" * 64],
    ),
)
def test_read_only_commands_never_migrate_schema_nine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    database_path = _schema_nine_database(tmp_path / "schema-nine.sqlite3")

    code, stdout, stderr = _invoke(
        capsys,
        ["--database-path", str(database_path), *command],
    )

    assert code == 6
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "database_not_ready",
    }
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 9
        objects = set(
            connection.execute(
                "SELECT type, name FROM sqlite_master"
            ).fetchall()
        )
    finally:
        connection.close()
    assert ("table", "platform_capabilities") in objects
    assert ("table", "capability_evidence") not in objects
    assert ("table", "capability_decisions") not in objects


def test_cli_rejects_malformed_csv_without_exposing_private_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = _initialized_database(tmp_path / "capability.sqlite3")
    manifest, results, sample_canaries = _write_stage0_bundle(tmp_path)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    lines[1] = ",".join(lines[1].split(",")[:-1])
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    code, stdout, stderr = _invoke(
        capsys,
        [
            "--database-path",
            str(database_path),
            "import",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
            "--environment-key",
            ENVIRONMENT,
        ],
    )

    assert code == 2
    assert stdout == ""
    assert json.loads(stderr) == {
        "status": "error",
        "error_code": "invalid_validation_input",
    }
    _assert_private_canaries_absent(
        stderr,
        manifest=manifest,
        results=results,
        sample_canaries=sample_canaries,
    )
