from __future__ import annotations

import csv
from pathlib import Path

import pytest

from video_download_control.validation import (
    CapabilityStatus,
    ValidationManifestError,
    evaluate_capabilities,
    load_validation_manifest,
    load_validation_results,
    render_capability_report,
)


MANIFEST_FIELDS = [
    "sample_id",
    "platform",
    "source_type",
    "url",
    "expected_outcome",
    "expected_media_count",
    "requires_cookie",
    "region",
]
RESULT_FIELDS = [
    "run_id",
    "sample_id",
    "observed_outcome",
    "observed_media_count",
    "completed_at",
    "adapter",
    "downloader_version",
    "environment",
]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def sample_rows() -> list[dict[str, object]]:
    rows = [
        {
            "sample_id": f"yt-positive-{index}",
            "platform": "youtube",
            "source_type": "youtube_video",
            "url": f"https://www.youtube.com/watch?v=sample{index}",
            "expected_outcome": "ready",
            "expected_media_count": 1,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
        for index in range(10)
    ]
    rows.append(
        {
            "sample_id": "yt-negative-1",
            "platform": "youtube",
            "source_type": "youtube_video",
            "url": "https://www.youtube.com/watch?v=unavailable-sample",
            "expected_outcome": "content_unavailable",
            "expected_media_count": 0,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
    )
    return rows


def result_rows(samples: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for run in range(1, 4):
        for sample in samples:
            rows.append(
                {
                    "run_id": f"run-{run}",
                    "sample_id": sample["sample_id"],
                    "observed_outcome": sample["expected_outcome"],
                    "observed_media_count": sample["expected_media_count"],
                    "completed_at": f"2026-09-0{run}T10:00:00+09:30",
                    "adapter": "yt_dlp",
                    "downloader_version": "2026.08.19",
                    "environment": "AU-SA-no-cookie",
                }
            )
    return rows


def test_three_complete_runs_produce_verified_report_without_raw_urls(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, result_rows(rows))
    samples = load_validation_manifest(manifest_path)
    results = load_validation_results(results_path)
    evidence = evaluate_capabilities(samples, results)
    assert len(evidence) == 1
    assert evidence[0].status is CapabilityStatus.VERIFIED
    assert evidence[0].last_three_positive_rates == (1.0, 1.0, 1.0)
    assert evidence[0].adapter == "yt_dlp"
    assert evidence[0].adapter_version == "2026.08.19"
    report = render_capability_report(samples, evidence)
    assert "verified" in report
    assert samples[0].url not in report
    assert samples[0].url_sha256 in report


def test_missing_or_failed_regression_stays_candidate(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    results = result_rows(rows)
    results = [
        row
        for row in results
        if not (row["run_id"] == "run-3" and row["sample_id"] == "yt-positive-9")
    ]
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, results)
    evidence = evaluate_capabilities(
        load_validation_manifest(manifest_path),
        load_validation_results(results_path),
    )
    assert evidence[0].status is CapabilityStatus.CANDIDATE
    assert evidence[0].complete_runs == 2


def test_manifest_rejects_declared_platform_mismatch_and_duplicate_url(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.csv"
    rows = sample_rows()[:1]
    rows[0]["platform"] = "x"
    write_csv(path, MANIFEST_FIELDS, rows)
    with pytest.raises(ValidationManifestError, match="platform"):
        load_validation_manifest(path)

    rows = sample_rows()[:2]
    rows[1]["url"] = rows[0]["url"]
    write_csv(path, MANIFEST_FIELDS, rows)
    with pytest.raises(ValidationManifestError, match="duplicate URL hash"):
        load_validation_manifest(path)


def test_runs_cannot_mix_adapter_version_or_environment(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    results = result_rows(rows)
    results[1]["adapter"] = "different-adapter"
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, results)

    with pytest.raises(ValidationManifestError, match="mixes"):
        evaluate_capabilities(
            load_validation_manifest(manifest_path),
            load_validation_results(results_path),
        )


def test_different_configurations_never_combine_into_verified_runs(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    results = result_rows(rows)
    for result in results:
        if result["run_id"] == "run-3":
            result["environment"] = "different-environment"
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, results)

    evidence = evaluate_capabilities(
        load_validation_manifest(manifest_path),
        load_validation_results(results_path),
    )

    assert len(evidence) == 2
    assert {item.status for item in evidence} == {CapabilityStatus.CANDIDATE}
