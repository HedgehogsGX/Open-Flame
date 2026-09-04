from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .capabilities import AdapterJobKind, AdapterRoute, CapabilityStatus
from .domain import ErrorCode, Platform, SourceType
from .normalization import URLNormalizationError, normalize_url


READY_OUTCOME = "ready"
STAGE0_POLICY_VERSION = "stage0-v3"
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
_IDENTITY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_PRODUCT_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class ValidationManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationSample:
    """One private Stage 0 input with route-specific output expectations.

    ``expected_output_count`` means published, fully verified media assets for
    ``download`` and unique source items committed to the immutable discovery
    snapshot for ``discover``.
    """

    sample_id: str
    platform: Platform
    source_type: SourceType
    job_kind: AdapterJobKind
    url: str = field(repr=False)
    source_id: str | None = field(repr=False)
    expected_outcome: str
    expected_output_count: int
    requires_cookie: bool
    region: str

    @property
    def url_sha256(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()

    @property
    def source_identity_sha256(self) -> str:
        value = self.source_id if self.source_id is not None else self.url
        framed = "\0".join(
            (self.platform.value, self.source_type.value, value)
        ).encode("utf-8")
        return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """One observed result using the same route-specific output definition."""

    run_id: str
    sample_id: str
    job_kind: AdapterJobKind
    observed_outcome: str
    observed_output_count: int
    completed_at: datetime
    adapter: str
    downloader_version: str
    environment: str
    product_version: str


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    platform: Platform
    source_type: SourceType
    job_kind: AdapterJobKind
    adapter: str
    downloader_version: str
    environment: str
    product_version: str
    positive_samples: int
    negative_samples: int
    complete_runs: int
    last_three_positive_rates: tuple[float, ...]
    last_three_negative_rates: tuple[float, ...]
    status: CapabilityStatus


def load_validation_manifest(path: Path) -> tuple[ValidationSample, ...]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationManifestError("validation manifest is unavailable") from exc
    return parse_validation_manifest(content)


def parse_validation_manifest(content: bytes) -> tuple[ValidationSample, ...]:
    text = _decode_utf8(content, kind="validation manifest")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {
        "sample_id",
        "platform",
        "source_type",
        "job_kind",
        "url",
        "expected_outcome",
        "expected_output_count",
        "requires_cookie",
        "region",
    }
    if (
        reader.fieldnames is None
        or len(reader.fieldnames) != len(required)
        or set(reader.fieldnames) != required
    ):
        raise ValidationManifestError(
            "validation manifest must contain the exact documented columns"
        )
    samples: list[ValidationSample] = []
    seen_ids: set[str] = set()
    seen_identities: set[tuple[AdapterJobKind, str]] = set()
    for line_number, row in enumerate(reader, start=2):
        _require_exact_row_shape(
            row, kind="validation manifest", line_number=line_number
        )
        try:
            sample = _sample_from_row(row)
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            URLNormalizationError,
        ) as exc:
            raise ValidationManifestError(
                f"invalid validation manifest row {line_number}: {exc}"
            ) from exc
        if sample.sample_id in seen_ids:
            raise ValidationManifestError(f"duplicate sample_id: {sample.sample_id}")
        source_identity = (sample.job_kind, sample.source_identity_sha256)
        if source_identity in seen_identities:
            raise ValidationManifestError(
                "duplicate source identity for job_kind at sample_id: "
                f"{sample.sample_id}"
            )
        seen_ids.add(sample.sample_id)
        seen_identities.add(source_identity)
        samples.append(sample)
    if not samples:
        raise ValidationManifestError("validation manifest is empty")
    return tuple(samples)


def load_validation_results(path: Path) -> tuple[ValidationResult, ...]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationManifestError("validation results are unavailable") from exc
    return parse_validation_results(content)


def parse_validation_results(content: bytes) -> tuple[ValidationResult, ...]:
    text = _decode_utf8(content, kind="validation results")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {
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
    }
    if (
        reader.fieldnames is None
        or len(reader.fieldnames) != len(required)
        or set(reader.fieldnames) != required
    ):
        raise ValidationManifestError(
            "validation results must contain the exact documented columns"
        )
    results: list[ValidationResult] = []
    seen: set[tuple[str, str]] = set()
    for line_number, row in enumerate(reader, start=2):
        _require_exact_row_shape(
            row, kind="validation result", line_number=line_number
        )
        try:
            result = _result_from_row(row)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
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
    run_configuration: dict[str, tuple[str, str, str, str]] = {}
    for result in results:
        sample = sample_by_id[result.sample_id]
        if result.job_kind is not sample.job_kind:
            raise ValidationManifestError(
                "result job_kind does not match its manifest sample: "
                f"{result.run_id}/{result.sample_id}"
            )
        configuration = (
            result.adapter,
            result.downloader_version,
            result.environment,
            result.product_version,
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
        tuple[Platform, SourceType, AdapterJobKind], list[ValidationSample]
    ] = defaultdict(list)
    for sample in samples:
        samples_by_capability[
            (sample.platform, sample.source_type, sample.job_kind)
        ].append(sample)

    evidence: list[CapabilityEvidence] = []
    for (platform, source_type, job_kind), capability_samples in sorted(
        samples_by_capability.items(),
        key=lambda item: (
            item[0][0].value,
            item[0][1].value,
            item[0][2].value,
        ),
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
        capability_sample_ids = {
            sample.sample_id for sample in capability_samples
        }
        relevant_runs = [
            run_id
            for run_id, run_results in results_by_run.items()
            if capability_sample_ids.intersection(run_results)
        ]
        runs_by_configuration: dict[
            tuple[str, str, str, str], list[str]
        ] = defaultdict(list)
        for run_id in relevant_runs:
            runs_by_configuration[run_configuration[run_id]].append(run_id)
        configurations = {
            run_configuration[run_id] for run_id in relevant_runs
        } or {
            ("unexecuted", "unexecuted", "unexecuted", "unexecuted")
        }
        for configuration in sorted(configurations):
            configured_runs = runs_by_configuration[configuration]
            configured_runs.sort(
                key=lambda run_id: (run_completed_at[run_id], run_id)
            )
            recent = configured_runs[-required_consecutive_runs:]
            complete_recent = [
                run_id
                for run_id in recent
                if all(
                    sample.sample_id in results_by_run[run_id]
                    for sample in capability_samples
                )
            ]
            positive_rates = tuple(
                _rate_for_run(
                    positives,
                    results_by_run[run_id],
                    require_output_count=True,
                )
                for run_id in complete_recent
            )
            negative_rates = tuple(
                _rate_for_run(
                    negatives,
                    results_by_run[run_id],
                    require_output_count=False,
                )
                for run_id in complete_recent
            )
            verified = (
                len(positives) >= minimum_positive_samples
                and len(recent) == required_consecutive_runs
                and len(complete_recent) == required_consecutive_runs
                and all(rate >= minimum_positive_rate for rate in positive_rates)
                and bool(negatives)
                and all(rate == 1.0 for rate in negative_rates)
            )
            adapter, downloader_version, environment, product_version = configuration
            evidence.append(
                CapabilityEvidence(
                    platform=platform,
                    source_type=source_type,
                    job_kind=job_kind,
                    adapter=adapter,
                    downloader_version=downloader_version,
                    environment=environment,
                    product_version=product_version,
                    positive_samples=len(positives),
                    negative_samples=len(negatives),
                    complete_runs=sum(
                        all(
                            sample.sample_id in results_by_run[run_id]
                            for sample in capability_samples
                        )
                        for run_id in configured_runs
                    ),
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
    del samples
    lines = [
        "# Platform capability evidence",
        "",
        "> Generated from local sample/result manifests. Raw URLs, URL hashes, sample IDs and run IDs are intentionally omitted.",
        "",
        "| Platform | Source type | Job kind | Adapter | Downloader | Product | Environment | Positive | Negative | Complete runs | Last positive rates | Last negative rates | Status |",
        "|---|---|---|---|---|---|---|---:|---:|---:|---|---|---|",
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
            f"{item.job_kind.value} | "
            f"{_markdown_cell(item.adapter)} | "
            f"{_markdown_cell(item.downloader_version)} | "
            f"{_markdown_cell(item.product_version)} | "
            f"{_markdown_cell(item.environment)} | "
            f"{item.positive_samples} | {item.negative_samples} | "
            f"{item.complete_runs} | {positive_rates} | {negative_rates} | "
            f"{item.status.value} |"
        )
    lines.append("")
    return "\n".join(lines)


def _sample_from_row(row: dict[str, str]) -> ValidationSample:
    sample_id = row["sample_id"].strip()
    if not _SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("sample_id is invalid")
    platform = Platform(row["platform"].strip())
    source_type = SourceType(row["source_type"].strip())
    job_kind = AdapterJobKind(row["job_kind"].strip())
    AdapterRoute(platform, source_type, job_kind)
    raw_url = row["url"].strip()
    normalized = normalize_url(raw_url)
    if normalized.platform != platform or normalized.source_type != source_type:
        raise ValueError("declared platform/source_type does not match the URL")
    if (
        platform is Platform.BILIBILI
        and normalized.source_id is not None
        and normalized.source_id.lower().startswith("av")
    ):
        raise ValueError("Stage 0 Bilibili samples must use BV identifiers")
    expected_outcome = row["expected_outcome"].strip()
    if expected_outcome not in ALLOWED_EXPECTED_OUTCOMES:
        raise ValueError("expected_outcome is not allowed")
    expected_output_count = int(row["expected_output_count"])
    if (
        expected_output_count < 0
        or (
            expected_outcome == READY_OUTCOME
            and expected_output_count < 1
        )
        or (
            expected_outcome != READY_OUTCOME
            and expected_output_count != 0
        )
    ):
        raise ValueError("expected_output_count is invalid")
    requires_cookie = _parse_bool(row["requires_cookie"])
    region = row["region"].strip()
    if not region or len(region) > 128:
        raise ValueError("region is required and must be at most 128 characters")
    return ValidationSample(
        sample_id=sample_id,
        platform=platform,
        source_type=source_type,
        job_kind=job_kind,
        url=normalized.canonical_url,
        source_id=normalized.source_id,
        expected_outcome=expected_outcome,
        expected_output_count=expected_output_count,
        requires_cookie=requires_cookie,
        region=region,
    )


def _result_from_row(row: dict[str, str]) -> ValidationResult:
    run_id = row["run_id"].strip()
    sample_id = row["sample_id"].strip()
    if not _SAMPLE_ID.fullmatch(run_id) or not _SAMPLE_ID.fullmatch(sample_id):
        raise ValueError("run_id or sample_id is invalid")
    job_kind = AdapterJobKind(row["job_kind"].strip())
    observed_outcome = row["observed_outcome"].strip()
    if observed_outcome not in ALLOWED_EXPECTED_OUTCOMES | {
        code.value for code in ErrorCode
    }:
        raise ValueError("observed_outcome is invalid")
    observed_output_count = int(row["observed_output_count"])
    if (
        observed_output_count < 0
        or (
            observed_outcome == READY_OUTCOME
            and observed_output_count < 1
        )
        or (
            observed_outcome != READY_OUTCOME
            and observed_output_count != 0
        )
    ):
        raise ValueError("observed_output_count is inconsistent with outcome")
    completed_at = datetime.fromisoformat(row["completed_at"].strip().replace("Z", "+00:00"))
    if completed_at.tzinfo is None:
        raise ValueError("completed_at must include a timezone")
    downloader_version = row["downloader_version"].strip()
    adapter = row["adapter"].strip()
    environment = row["environment"].strip()
    product_version = row["product_version"].strip()
    if not _IDENTITY_TOKEN.fullmatch(adapter):
        raise ValueError("adapter must be a safe identity token")
    if not _IDENTITY_TOKEN.fullmatch(downloader_version):
        raise ValueError("downloader_version must be a safe identity token")
    if not _ENVIRONMENT_KEY.fullmatch(environment):
        raise ValueError("environment must be a safe identity token")
    if not _PRODUCT_IDENTITY.fullmatch(product_version):
        raise ValueError("product_version must be a safe identity token")
    return ValidationResult(
        run_id=run_id,
        sample_id=sample_id,
        job_kind=job_kind,
        observed_outcome=observed_outcome,
        observed_output_count=observed_output_count,
        completed_at=completed_at,
        adapter=adapter,
        downloader_version=downloader_version,
        environment=environment,
        product_version=product_version,
    )


def _rate_for_run(
    samples: list[ValidationSample],
    results: dict[str, ValidationResult],
    *,
    require_output_count: bool,
) -> float:
    if not samples:
        return 0.0
    matches = 0
    for sample in samples:
        result = results[sample.sample_id]
        outcome_matches = result.observed_outcome == sample.expected_outcome
        count_matches = (
            result.observed_output_count == sample.expected_output_count
            if require_output_count
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


def _require_exact_row_shape(
    row: dict[str | None, object], *, kind: str, line_number: int
) -> None:
    if None in row or any(value is None for value in row.values()):
        raise ValidationManifestError(
            f"invalid {kind} row {line_number}: "
            "column count does not match header"
        )


def _decode_utf8(content: bytes, *, kind: str) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationManifestError(f"{kind} must be UTF-8") from exc


def _markdown_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
