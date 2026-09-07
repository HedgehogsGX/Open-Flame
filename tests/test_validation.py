from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

import video_download_control.validation_cli as validation_cli_module
from video_download_control.build_identity import (
    ProductBuildDriftError,
    ProductBuildUnavailableError,
)
from video_download_control.validation import (
    CapabilityStatus,
    ValidationManifestError,
    evaluate_capabilities,
    load_validation_manifest,
    load_validation_results,
    render_capability_report,
)
from video_download_control.validation_cli import main as validation_main


MANIFEST_FIELDS = [
    "sample_id",
    "platform",
    "source_type",
    "job_kind",
    "url",
    "expected_outcome",
    "expected_output_count",
    "requires_cookie",
    "region",
]
RESULT_FIELDS = [
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
            "job_kind": "download",
            "url": f"https://www.youtube.com/watch?v=sample{index}",
            "expected_outcome": "ready",
            "expected_output_count": 1,
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
            "job_kind": "download",
            "url": "https://www.youtube.com/watch?v=unavailable-sample",
            "expected_outcome": "content_unavailable",
            "expected_output_count": 0,
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
                    "job_kind": sample["job_kind"],
                    "observed_outcome": sample["expected_outcome"],
                    "observed_output_count": sample["expected_output_count"],
                    "completed_at": f"2026-09-0{run}T10:00:00+09:30",
                    "adapter": "yt_dlp",
                    "downloader_version": "2026.08.19",
                    "environment": "AU-SA-no-cookie",
                    "product_version": "0.14.0",
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
    assert evidence[0].job_kind.value == "download"
    assert evidence[0].downloader_version == "2026.08.19"
    assert evidence[0].product_version == "0.14.0"
    report = render_capability_report(samples, evidence)
    assert "verified" in report
    assert "| Job kind |" in report
    assert "| Downloader | Product | Environment |" in report
    assert samples[0].url not in report
    assert samples[0].sample_id not in report
    assert samples[0].url_sha256 not in report


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


def test_latest_partial_run_fails_closed_instead_of_using_older_complete_runs(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    results = result_rows(rows)
    latest_partial = [
        {
            **row,
            "run_id": "run-4",
            "completed_at": "2026-09-04T10:00:00+09:30",
        }
        for row in results
        if row["run_id"] == "run-3" and row["sample_id"] != "yt-positive-9"
    ]
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, [*results, *latest_partial])

    evidence = evaluate_capabilities(
        load_validation_manifest(manifest_path),
        load_validation_results(results_path),
    )

    assert len(evidence) == 1
    assert evidence[0].status is CapabilityStatus.CANDIDATE
    assert evidence[0].complete_runs == 3
    assert evidence[0].last_three_positive_rates == (1.0, 1.0)


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
    with pytest.raises(ValidationManifestError, match="duplicate source identity"):
        load_validation_manifest(path)


def test_manifest_rejects_canonical_url_aliases_as_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "manifest.csv"
    rows = sample_rows()[:2]
    rows[0]["url"] = (
        "https://www.youtube.com/watch?v=canonical-alias&feature=shared"
    )
    rows[1]["url"] = "https://youtu.be/canonical-alias?t=30"
    write_csv(path, MANIFEST_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="duplicate source identity"):
        load_validation_manifest(path)


def test_manifest_rejects_tiktok_handle_and_host_aliases_for_the_same_video(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.csv"
    rows = sample_rows()[:2]
    for row in rows:
        row["platform"] = "tiktok"
        row["source_type"] = "tiktok_video"
    rows[0]["url"] = (
        "https://www.tiktok.com/@First.Handle/video/7123456789012345678?lang=en"
    )
    rows[1]["url"] = (
        "https://m.tiktok.com/@second_handle/video/7123456789012345678"
    )
    write_csv(path, MANIFEST_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="duplicate source identity"):
        load_validation_manifest(path)


def test_stage0_rejects_bilibili_av_aliases_instead_of_double_counting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.csv"
    rows = sample_rows()[:1]
    rows[0]["platform"] = "bilibili"
    rows[0]["source_type"] = "bilibili_video"
    # The pinned yt-dlp fixture identifies BV13x41117TL and av8903802 as
    # locators for the same submission.  Stage 0 accepts only the BV namespace
    # so the two forms can never be counted as independent positive samples.
    rows[0]["url"] = "https://www.bilibili.com/video/BV13x41117TL"
    write_csv(path, MANIFEST_FIELDS, rows)

    accepted = load_validation_manifest(path)
    assert accepted[0].source_id == "BV13x41117TL"

    rows[0]["url"] = "https://www.bilibili.com/video/av8903802"
    write_csv(path, MANIFEST_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="must use BV"):
        load_validation_manifest(path)


def test_manifest_rejects_numeric_leading_zero_source_aliases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.csv"
    rows = sample_rows()[:2]
    for row in rows:
        row["platform"] = "x"
        row["source_type"] = "x_post"
    rows[0]["url"] = "https://x.com/one/status/1234567890"
    rows[1]["url"] = "https://twitter.com/two/status/0001234567890"
    write_csv(path, MANIFEST_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="duplicate source identity"):
        load_validation_manifest(path)


@pytest.mark.parametrize(
    "identity_field",
    ("adapter", "downloader_version", "environment", "product_version"),
)
def test_runs_cannot_mix_identity_fields(
    tmp_path: Path,
    identity_field: str,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    results = result_rows(rows)
    results[1][identity_field] = "different-identity"
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, results)

    with pytest.raises(ValidationManifestError, match="mixes"):
        evaluate_capabilities(
            load_validation_manifest(manifest_path),
            load_validation_results(results_path),
        )


@pytest.mark.parametrize(
    ("identity_field", "invalid_value"),
    (
        ("adapter", "a" * 65),
        ("adapter", "yt/dlp"),
        ("downloader_version", "v" * 65),
        ("downloader_version", "https://version.invalid"),
        ("environment", "e" * 129),
        ("environment", r"C:\private\stage0"),
        ("product_version", "p" * 129),
        ("product_version", "0.13.0/private"),
    ),
)
def test_result_identity_tokens_reject_overlong_or_unsafe_values(
    tmp_path: Path,
    identity_field: str,
    invalid_value: str,
) -> None:
    path = tmp_path / "results.csv"
    rows = result_rows(sample_rows()[:1])[:1]
    rows[0][identity_field] = invalid_value
    write_csv(path, RESULT_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="safe identity token"):
        load_validation_results(path)


def test_product_version_is_required_in_results_schema(tmp_path: Path) -> None:
    path = tmp_path / "results.csv"
    legacy_fields = [field for field in RESULT_FIELDS if field != "product_version"]
    rows = [
        {field: value for field, value in row.items() if field in legacy_fields}
        for row in result_rows(sample_rows()[:1])[:1]
    ]
    write_csv(path, legacy_fields, rows)

    with pytest.raises(ValidationManifestError, match="exact documented columns"):
        load_validation_results(path)


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


def test_job_kind_is_required_and_result_must_match_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    legacy_manifest_fields = [
        "sample_id",
        "platform",
        "source_type",
        "url",
        "expected_outcome",
        "expected_media_count",
        "requires_cookie",
        "region",
    ]
    legacy_manifest_rows = [
        {
            **{
                field: row[field]
                for field in legacy_manifest_fields
                if field != "expected_media_count"
            },
            "expected_media_count": row["expected_output_count"],
        }
        for row in rows
    ]
    write_csv(manifest_path, legacy_manifest_fields, legacy_manifest_rows)
    with pytest.raises(ValidationManifestError, match="exact documented columns"):
        load_validation_manifest(manifest_path)

    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    result_data = result_rows(rows)
    legacy_result_fields = [
        "run_id",
        "sample_id",
        "observed_outcome",
        "observed_media_count",
        "completed_at",
        "adapter",
        "downloader_version",
        "environment",
    ]
    legacy_result_rows = [
        {
            **{
                field: row[field]
                for field in legacy_result_fields
                if field != "observed_media_count"
            },
            "observed_media_count": row["observed_output_count"],
        }
        for row in result_data
    ]
    write_csv(results_path, legacy_result_fields, legacy_result_rows)
    with pytest.raises(ValidationManifestError, match="exact documented columns"):
        load_validation_results(results_path)

    result_data[0]["job_kind"] = "discover"
    write_csv(results_path, RESULT_FIELDS, result_data)
    with pytest.raises(ValidationManifestError, match="job_kind does not match"):
        evaluate_capabilities(
            load_validation_manifest(manifest_path),
            load_validation_results(results_path),
        )


def test_manifest_rejects_job_kind_that_is_not_valid_for_route(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    rows = sample_rows()[:1]
    rows[0]["job_kind"] = "discover"
    write_csv(manifest_path, MANIFEST_FIELDS, rows)

    with pytest.raises(ValidationManifestError, match="combination is invalid"):
        load_validation_manifest(manifest_path)


def test_output_count_must_match_ready_or_terminal_outcome_shape(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    manifest_rows = sample_rows()[:1]
    manifest_rows[0]["expected_outcome"] = "content_unavailable"
    manifest_rows[0]["expected_output_count"] = 1
    write_csv(manifest_path, MANIFEST_FIELDS, manifest_rows)

    with pytest.raises(ValidationManifestError, match="expected_output_count"):
        load_validation_manifest(manifest_path)

    manifest_rows[0]["expected_outcome"] = "ready"
    manifest_rows[0]["expected_output_count"] = 1
    write_csv(manifest_path, MANIFEST_FIELDS, manifest_rows)
    result_data = result_rows(manifest_rows)[:1]
    result_data[0]["observed_outcome"] = "content_unavailable"
    result_data[0]["observed_output_count"] = 1
    write_csv(results_path, RESULT_FIELDS, result_data)

    with pytest.raises(ValidationManifestError, match="observed_output_count"):
        load_validation_results(results_path)

    result_data[0]["observed_outcome"] = "ready"
    result_data[0]["observed_output_count"] = 0
    write_csv(results_path, RESULT_FIELDS, result_data)

    with pytest.raises(ValidationManifestError, match="observed_output_count"):
        load_validation_results(results_path)


def test_discover_and_download_are_separate_evidence_for_the_same_urls(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows: list[dict[str, object]] = []
    for job_kind, positive_count in (("discover", 2), ("download", 1)):
        rows.extend(
            [
                {
                    "sample_id": f"x-{job_kind}-positive",
                    "platform": "x",
                    "source_type": "x_post",
                    "job_kind": job_kind,
                    "url": "https://x.com/example/status/100",
                    "expected_outcome": "ready",
                    "expected_output_count": positive_count,
                    "requires_cookie": "false",
                    "region": "AU-SA",
                },
                {
                    "sample_id": f"x-{job_kind}-negative",
                    "platform": "x",
                    "source_type": "x_post",
                    "job_kind": job_kind,
                    "url": "https://x.com/example/status/101",
                    "expected_outcome": "content_unavailable",
                    "expected_output_count": 0,
                    "requires_cookie": "false",
                    "region": "AU-SA",
                },
            ]
        )
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, result_rows(rows)[: len(rows)])

    evidence = evaluate_capabilities(
        load_validation_manifest(manifest_path),
        load_validation_results(results_path),
        minimum_positive_samples=1,
        required_consecutive_runs=1,
    )

    assert len(evidence) == 2
    assert {item.job_kind.value for item in evidence} == {"discover", "download"}
    assert {item.status for item in evidence} == {CapabilityStatus.VERIFIED}


def test_unrelated_run_configuration_is_not_emitted_for_unexecuted_cell(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.csv"
    results_path = tmp_path / "results.csv"
    rows = sample_rows()
    rows.append(
        {
            "sample_id": "x-unexecuted",
            "platform": "x",
            "source_type": "x_post",
            "job_kind": "download",
            "url": "https://x.com/example/status/102",
            "expected_outcome": "ready",
            "expected_output_count": 1,
            "requires_cookie": "false",
            "region": "AU-SA",
        }
    )
    write_csv(manifest_path, MANIFEST_FIELDS, rows)
    write_csv(results_path, RESULT_FIELDS, result_rows(rows[:-1]))

    evidence = evaluate_capabilities(
        load_validation_manifest(manifest_path),
        load_validation_results(results_path),
    )
    x_evidence = next(item for item in evidence if item.platform.value == "x")

    assert x_evidence.adapter == "unexecuted"
    assert x_evidence.complete_runs == 0


@pytest.mark.parametrize("kind", ("manifest", "results"))
@pytest.mark.parametrize("shape", ("short", "extra"))
def test_csv_rows_must_have_the_exact_header_width(
    tmp_path: Path,
    kind: str,
    shape: str,
) -> None:
    path = tmp_path / f"{kind}-{shape}.csv"
    fields = MANIFEST_FIELDS if kind == "manifest" else RESULT_FIELDS
    rows = sample_rows()[:1] if kind == "manifest" else result_rows(sample_rows()[:1])[:1]
    write_csv(path, fields, rows)
    lines = path.read_text(encoding="utf-8").splitlines()
    if shape == "short":
        lines[1] = ",".join(lines[1].split(",")[:-1])
    else:
        lines[1] += ",EXTRA-CANARY"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    loader = load_validation_manifest if kind == "manifest" else load_validation_results
    with pytest.raises(ValidationManifestError, match="column count"):
        loader(path)


@pytest.mark.parametrize("kind", ("manifest", "results"))
def test_csv_duplicate_headers_are_rejected_as_ambiguous(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / f"{kind}-duplicate-header.csv"
    fields = MANIFEST_FIELDS if kind == "manifest" else RESULT_FIELDS
    rows = sample_rows()[:1] if kind == "manifest" else result_rows(sample_rows()[:1])[:1]
    write_csv(path, fields, rows)
    lines = path.read_text(encoding="utf-8").splitlines()
    duplicate = "url" if kind == "manifest" else "observed_outcome"
    lines[0] += f",{duplicate}"
    lines[1] += ",AMBIGUOUS-CANARY"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    loader = load_validation_manifest if kind == "manifest" else load_validation_results
    with pytest.raises(ValidationManifestError, match="exact documented columns"):
        loader(path)


def test_validation_cli_reports_current_build_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = validation_main(["--print-product-identity"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["product_version"] == "0.26.0"
    assert re.fullmatch(
        re.escape(payload["product_version"]) + r"\+build\.sha256\.[0-9a-f]{64}",
        payload["product_identity"],
    )


def test_validation_cli_sanitizes_malformed_row_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "PRIVATE-PATH-CANARY.csv"
    write_csv(path, MANIFEST_FIELDS, sample_rows()[:1])
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = ",".join(lines[1].split(",")[:-1])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    code = validation_main([str(path)])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_code": "invalid_validation_input",
    }
    assert "PRIVATE-PATH-CANARY" not in captured.err


def test_validation_cli_sanitizes_invalid_argument_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = validation_main(
        ["--PRIVATE-OPTION-CANARY", r"C:\Users\PRIVATE-PATH-CANARY"]
    )
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_code": "invalid_arguments",
    }
    assert "PRIVATE" not in captured.err


@pytest.mark.parametrize(
    ("error", "error_code"),
    (
        (
            ProductBuildDriftError(r"C:\Users\PRIVATE-DRIFT-CANARY"),
            "product_build_drift",
        ),
        (
            ProductBuildUnavailableError(
                r"C:\Users\PRIVATE-UNAVAILABLE-CANARY"
            ),
            "product_build_unavailable",
        ),
    ),
)
def test_validation_cli_sanitizes_product_identity_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: RuntimeError,
    error_code: str,
) -> None:
    def fail_identity() -> str:
        raise error

    monkeypatch.setattr(
        validation_cli_module, "current_product_identity", fail_identity
    )

    code = validation_main(["--print-product-identity"])
    captured = capsys.readouterr()

    assert code == 6
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_code": error_code,
    }
    assert "PRIVATE" not in captured.err
