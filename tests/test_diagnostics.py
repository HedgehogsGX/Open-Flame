from __future__ import annotations

from pathlib import Path

from video_download_control.adapters import AdapterFailure, ScriptedFakeAdapter
from video_download_control.assets import (
    AssetStore,
    AssetValidationError,
    NonEmptyTestVerifier,
)
from video_download_control.diagnostics import (
    MAX_DIAGNOSTIC_LENGTH,
    sanitize_diagnostic,
)
from video_download_control.domain import ErrorCode
from video_download_control.worker import Worker
from video_download_control.worker_repository import WorkerRepository


SECRETS = {
    "bearer-secret",
    "cookie-secret",
    "query-token",
    "signed-secret",
    "json-secret",
    "still-secret",
}


def _sensitive_diagnostic() -> str:
    return (
        "upstream rejected request\n"
        "Authorization: Bearer bearer-secret\n"
        "Cookie: sid=cookie-secret; theme=dark\n"
        "url=https://user:password@cdn.example/video?"
        "token=query-token&signature=signed-secret\n"
        '{"token": "json-secret\\\"still-secret", "reason": "expired"}'
    )


def test_central_diagnostic_sanitizer_redacts_common_credentials() -> None:
    sanitized = sanitize_diagnostic(_sensitive_diagnostic())

    assert sanitized.startswith("upstream rejected request")
    assert "reason" in sanitized
    assert "[REDACTED]" in sanitized
    assert "user:password" not in sanitized
    assert all(secret not in sanitized for secret in SECRETS)
    assert sanitize_diagnostic(sanitized) == sanitized


def test_central_diagnostic_sanitizer_bounds_after_redaction() -> None:
    sanitized = sanitize_diagnostic(
        "Authorization: Bearer do-not-store\n" + "x" * 5000
    )

    assert len(sanitized) == MAX_DIAGNOSTIC_LENGTH
    assert "do-not-store" not in sanitized


def test_adapter_failure_sanitizes_before_crossing_worker_boundary() -> None:
    failure = AdapterFailure(ErrorCode.NETWORK_ERROR, _sensitive_diagnostic())

    assert all(secret not in failure.diagnostic for secret in SECRETS)
    assert failure.sanitized_detail == failure.diagnostic


class _SensitiveVerifier(NonEmptyTestVerifier):
    def verify(self, path: Path, media_kind: str):
        del path, media_kind
        raise AssetValidationError(_sensitive_diagnostic())


def test_worker_failure_diagnostic_is_sanitized_before_database_storage(
    service, settings, database
) -> None:
    batch = service.create_batch(
        name="redaction boundary",
        raw_inputs=["https://www.youtube.com/watch?v=redact-storage"],
    )
    job_id = batch["jobs"][0]["id"]
    repository = WorkerRepository(database)
    worker = Worker(
        worker_id="offline-test-worker",
        repository=repository,
        adapter=ScriptedFakeAdapter(),
        asset_store=AssetStore(settings.data_root),
        verifier=_SensitiveVerifier(),
    )

    result = worker.run_once()

    assert result is not None
    stored = repository.attempts_for(job_id)[0]["diagnostic"]
    assert all(secret not in stored for secret in SECRETS)
    assert "[REDACTED]" in stored
