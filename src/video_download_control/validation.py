from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .domain import ErrorCode, Platform, SourceType
from .normalization import URLNormalizationError, normalize_url


READY_OUTCOME = "ready"
ALLOWED_EXPECTED_OUTCOMES = frozenset(
    {
        READY_OUTCOME,
        ErrorCode.AUTHENTICATION_REQUIRED.value,
        ErrorCode.PRIVATE_CONTENT.value,
        ErrorCode.CONTENT_UNAVAILABLE.value,
        ErrorCode.GEO_RESTRICTED.value,
        ErrorCode.DRM_PROTECTED.value,
    }
)
_SAMPLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CapabilityStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"


class ValidationManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationSample:
    sample_id: str
    platform: Platform
    source_type: SourceType
    url: str = field(repr=False)
    expected_outcome: str
    expected_media_count: int
    requires_cookie: bool
    region: str

    @property
    def url_sha256(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    run_id: str
    sample_id: str
    observed_outcome: str
    observed_media_count: int
    completed_at: datetime
    adapter: str
    downloader_version: str
    environment: str


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    platform: Platform
    source_type: SourceType
    adapter: str
    adapter_version: str
    environment: str
    positive_samples: int
    negative_samples: int
    complete_runs: int
    last_three_positive_rates: tuple[float, ...]
    last_three_negative_rates: tuple[float, ...]
    status: CapabilityStatus


def load_validation_manifest(path: Path) -> tuple[ValidationSample, ...]:
    text = _read_utf8(path)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {
        "sample_id",
        "platform",
        "source_type",
        "url",
        "expected_outcome",
        "expected_media_count",
        "requires_cookie",
        "region",
    }
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise ValidationManifestError(
            "validation manifest must contain the exact documented columns"
        )
    samples: list[ValidationSample] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        try:
            sample = _sample_from_row(row)
        except (KeyError, ValueError, URLNormalizationError) as exc:
            raise ValidationManifestError(
                f"invalid validation manifest row {line_number}: {exc}"
            ) from exc
        if sample.sample_id in seen_ids:
            raise ValidationManifestError(f"duplicate sample_id: {sample.sample_id}")
        if sample.url_sha256 in seen_hashes:
            raise ValidationManifestError(
                f"duplicate URL hash at sample_id: {sample.sample_id}"
            )
        seen_ids.add(sample.sample_id)
        seen_hashes.add(sample.url_sha256)
        samples.append(sample)
    if not samples:
        raise ValidationManifestError("validation manifest is empty")
    return tuple(samples)


def load_validation_results(path: Path) -> tuple[ValidationResult, ...]:
    text = _read_utf8(path)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {
        "run_id",
        "sample_id",
        "observed_outcome",
        "observed_media_count",
        "completed_at",
        "adapter",
        "downloader_version",
        "environment",
    }
    if reader.fieldnames is None or set(reader.fieldnames) != required:
        raise ValidationManifestError(
            "validation results must contain the exact documented columns"
        )
    results: list[ValidationResult] = []
    seen: set[tuple[str, str]] = set()
    for line_number, row in enumerate(reader, start=2):
        try:
            result = _result_from_row(row)
        except (KeyError, ValueError) as exc:
            raise ValidationManifestError(
                f"invalid validation result row {line_number}: {exc}"
            ) from exc
        identity = (result.run_id, result.sample_id)
        if identity in seen:
            raise ValidationManifestError(
                f"duplicate result for run/sample: {result.run_id}/{result.sample_id}"
            )
        seen.add(identity)
        results.append(result)
    return tuple(results)


def evaluate_capabilities(
    samples: tuple[ValidationSample, ...],
    results: tuple[ValidationResult, ...],
    *,
    minimum_positive_samples: int = 10,
    required_consecutive_runs: int = 3,
    minimum_positive_rate: float = 0.9,
) -> tuple[CapabilityEvidence, ...]:
    sample_by_id = {sample.sample_id: sample for sample in samples}
    unknown_ids = {result.sample_id for result in results} - set(sample_by_id)
    if unknown_ids:
        raise ValidationManifestError(
            f"results reference unknown sample IDs: {', '.join(sorted(unknown_ids))}"
        )
    results_by_run: dict[str, dict[str, ValidationResult]] = defaultdict(dict)
    run_completed_at: dict[str, datetime] = {}
    run_configuration: dict[str, tuple[str, str, str]] = {}
    for result in results:
        configuration = (
            result.adapter,
            result.downloader_version,
            result.environment,
        )
        previous_configuration = run_configuration.setdefault(
            result.run_id, configuration
        )
        if previous_configuration != configuration:
            raise ValidationManifestError(
                f"run mixes adapter/version/environment: {result.run_id}"
            )
        results_by_run[result.run_id][result.sample_id] = result
        run_completed_at[result.run_id] = max(
            run_completed_at.get(result.run_id, result.completed_at),
            result.completed_at,
        )

    samples_by_capability: dict[
        tuple[Platform, SourceType], list[ValidationSample]
    ] = defaultdict(list)
    for sample in samples:
        samples_by_capability[(sample.platform, sample.source_type)].append(sample)

    evidence: list[CapabilityEvidence] = []
    for (platform, source_type), capability_samples in sorted(
        samples_by_capability.items(), key=lambda item: (item[0][0].value, item[0][1].value)
    ):
        positives = [
            sample
            for sample in capability_samples
            if sample.expected_outcome == READY_OUTCOME and not sample.requires_cookie
        ]
        negatives = [
            sample
            for sample in capability_samples
            if sample.expected_outcome != READY_OUTCOME
        ]
        complete_runs = [
            run_id
            for run_id, run_results in results_by_run.items()
            if all(sample.sample_id in run_results for sample in capability_samples)
        ]
        runs_by_configuration: dict[
            tuple[str, str, str], list[str]
        ] = defaultdict(list)
        for run_id in complete_runs:
            runs_by_configuration[run_configuration[run_id]].append(run_id)
        configurations = set(run_configuration.values()) or {
            ("unexecuted", "unexecuted", "unexecuted")
        }
        for configuration in sorted(configurations):
            configured_runs = runs_by_configuration[configuration]
            configured_runs.sort(
                key=lambda run_id: (run_completed_at[run_id], run_id)
            )
            recent = configured_runs[-required_consecutive_runs:]
            positive_rates = tuple(
                _rate_for_run(
                    positives,
                    results_by_run[run_id],
                    require_media_count=True,
                )
                for run_id in recent
            )
            negative_rates = tuple(
                _rate_for_run(
                    negatives,
                    results_by_run[run_id],
                    require_media_count=False,
                )
                for run_id in recent
            )
            verified = (
                len(positives) >= minimum_positive_samples
                and len(recent) == required_consecutive_runs
                and all(rate >= minimum_positive_rate for rate in positive_rates)
                and bool(negatives)
                and all(rate == 1.0 for rate in negative_rates)
            )
            adapter, adapter_version, environment = configuration
            evidence.append(
                CapabilityEvidence(
                    platform=platform,
                    source_type=source_type,
                    adapter=adapter,
                    adapter_version=adapter_version,
                    environment=environment,
                    positive_samples=len(positives),
                    negative_samples=len(negatives),
                    complete_runs=len(configured_runs),
                    last_three_positive_rates=positive_rates,
                    last_three_negative_rates=negative_rates,
                    status=(
                        CapabilityStatus.VERIFIED
                        if verified
                        else CapabilityStatus.CANDIDATE
                    ),
                )
            )
    return tuple(evidence)


def render_capability_report(
    samples: tuple[ValidationSample, ...],
    evidence: tuple[CapabilityEvidence, ...],
) -> str:
    lines = [
        "# Platform capability evidence",
        "",
        "> Generated from local sample/result manifests. Raw URLs are intentionally omitted.",
        "",
        "| Platform | Source type | Adapter | Version | Environment | Positive | Negative | Complete runs | Last positive rates | Last negative rates | Status |",
        "|---|---|---|---|---|---:|---:|---:|---|---|---|",
    ]
    for item in evidence:
        positive_rates = ", ".join(
            f"{rate:.0%}" for rate in item.last_three_positive_rates
        ) or "none"
        negative_rates = ", ".join(
            f"{rate:.0%}" for rate in item.last_three_negative_rates
        ) or "none"
        lines.append(
            f"| {item.platform.value} | {item.source_type.value} | "
            f"{_markdown_cell(item.adapter)} | "
            f"{_markdown_cell(item.adapter_version)} | "
            f"{_markdown_cell(item.environment)} | "
            f"{item.positive_samples} | {item.negative_samples} | "
            f"{item.complete_runs} | {positive_rates} | {negative_rates} | "
            f"{item.status.value} |"
        )
    lines.extend(
        [
            "",
            "## Sample index",
            "",
            "| Sample ID | URL SHA-256 | Platform | Source type | Expected outcome | Expected media | Cookie | Region |",
            "|---|---|---|---|---|---:|---|---|",
        ]
    )
    for sample in samples:
        lines.append(
            f"| {sample.sample_id} | `{sample.url_sha256}` | "
            f"{sample.platform.value} | {sample.source_type.value} | "
            f"{sample.expected_outcome} | {sample.expected_media_count} | "
            f"{'yes' if sample.requires_cookie else 'no'} | "
            f"{_markdown_cell(sample.region)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _sample_from_row(row: dict[str, str]) -> ValidationSample:
    sample_id = row["sample_id"].strip()
    if not _SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("sample_id is invalid")
    platform = Platform(row["platform"].strip())
    source_type = SourceType(row["source_type"].strip())
    url = row["url"].strip()
    normalized = normalize_url(url)
    if normalized.platform != platform or normalized.source_type != source_type:
        raise ValueError("declared platform/source_type does not match the URL")
    expected_outcome = row["expected_outcome"].strip()
    if expected_outcome not in ALLOWED_EXPECTED_OUTCOMES:
        raise ValueError("expected_outcome is not allowed")
    expected_media_count = int(row["expected_media_count"])
    if expected_media_count < 0 or (
        expected_outcome == READY_OUTCOME and expected_media_count < 1
    ):
        raise ValueError("expected_media_count is invalid")
    requires_cookie = _parse_bool(row["requires_cookie"])
    region = row["region"].strip()
    if not region or len(region) > 128:
        raise ValueError("region is required and must be at most 128 characters")
    return ValidationSample(
        sample_id=sample_id,
        platform=platform,
        source_type=source_type,
        url=url,
        expected_outcome=expected_outcome,
        expected_media_count=expected_media_count,
        requires_cookie=requires_cookie,
        region=region,
    )


def _result_from_row(row: dict[str, str]) -> ValidationResult:
    run_id = row["run_id"].strip()
    sample_id = row["sample_id"].strip()
    if not _SAMPLE_ID.fullmatch(run_id) or not _SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("run_id or sample_id is invalid")
    observed_outcome = row["observed_outcome"].strip()
    if observed_outcome not in ALLOWED_EXPECTED_OUTCOMES | {
        code.value for code in ErrorCode
    }:
        raise ValueError("observed_outcome is invalid")
    observed_media_count = int(row["observed_media_count"])
    if observed_media_count < 0:
        raise ValueError("observed_media_count must be non-negative")
    completed_at = datetime.fromisoformat(row["completed_at"].strip().replace("Z", "+00:00"))
    if completed_at.tzinfo is None:
        raise ValueError("completed_at must include a timezone")
    downloader_version = row["downloader_version"].strip()
    adapter = row["adapter"].strip()
    environment = row["environment"].strip()
    if not adapter or not downloader_version or not environment:
        raise ValueError("adapter, downloader_version and environment are required")
    return ValidationResult(
        run_id=run_id,
        sample_id=sample_id,
        observed_outcome=observed_outcome,
        observed_media_count=observed_media_count,
        completed_at=completed_at,
        adapter=adapter[:128],
        downloader_version=downloader_version[:128],
        environment=environment[:128],
    )


def _rate_for_run(
    samples: list[ValidationSample],
    results: dict[str, ValidationResult],
    *,
    require_media_count: bool,
) -> float:
    if not samples:
        return 0.0
    matches = 0
    for sample in samples:
        result = results[sample.sample_id]
        outcome_matches = result.observed_outcome == sample.expected_outcome
        count_matches = (
            result.observed_media_count == sample.expected_media_count
            if require_media_count
            else True
        )
        matches += outcome_matches and count_matches
    return matches / len(samples)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError("boolean field must be true/false")


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationManifestError(f"{path.name} must be UTF-8") from exc


def _markdown_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
