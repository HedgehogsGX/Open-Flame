from __future__ import annotations

import pytest

from video_download_control.domain import ErrorCode
from video_download_control.retry_policy import (
    RetryAction,
    RetryContext,
    RetryPolicy,
    decide_retry,
)


@pytest.fixture
def policy() -> RetryPolicy:
    return RetryPolicy()


@pytest.mark.parametrize(
    ("code", "first_delay", "second_delay"),
    [
        (ErrorCode.NETWORK_ERROR, 5.0, 10.0),
        (ErrorCode.RATE_LIMITED, 60.0, 120.0),
    ],
)
def test_transient_errors_retry_twice_with_exponential_backoff(
    policy: RetryPolicy,
    code: ErrorCode,
    first_delay: float,
    second_delay: float,
) -> None:
    first = policy.decide(code, RetryContext(1, 1))
    second = policy.decide(code, RetryContext(2, 2))
    exhausted = policy.decide(code, RetryContext(3, 3))

    assert (first.action, first.delay_seconds) == (RetryAction.RETRY, first_delay)
    assert (second.action, second.delay_seconds) == (
        RetryAction.RETRY,
        second_delay,
    )
    assert exhausted.action is RetryAction.TERMINAL


def test_retry_after_can_extend_but_not_shorten_policy_delay(
    policy: RetryPolicy,
) -> None:
    extended = policy.decide(
        ErrorCode.RATE_LIMITED,
        RetryContext(1, 1, retry_after_seconds=300),
    )
    not_shortened = policy.decide(
        ErrorCode.RATE_LIMITED,
        RetryContext(1, 1, retry_after_seconds=10),
    )

    assert extended.delay_seconds == 300
    assert not_shortened.delay_seconds == 60


def test_validation_failure_retries_once(policy: RetryPolicy) -> None:
    retry = policy.decide(ErrorCode.VALIDATION_FAILED, RetryContext(1, 1))
    exhausted = policy.decide(ErrorCode.VALIDATION_FAILED, RetryContext(2, 2))

    assert retry.action is RetryAction.RETRY
    assert retry.delay_seconds == 0
    assert exhausted.action is RetryAction.TERMINAL


@pytest.mark.parametrize(
    "code",
    [ErrorCode.ADAPTER_UNSUPPORTED, ErrorCode.EXTRACTOR_BROKEN],
)
def test_only_routed_errors_may_use_one_explicit_fallback(
    policy: RetryPolicy,
    code: ErrorCode,
) -> None:
    fallback = policy.decide(
        code,
        RetryContext(1, 1, fallback_available=True),
    )
    no_fallback = policy.decide(code, RetryContext(1, 1))
    already_used = policy.decide(
        code,
        RetryContext(
            2,
            1,
            fallback_available=True,
            fallback_already_used=True,
        ),
    )

    assert fallback.action is RetryAction.FALLBACK
    assert no_fallback.action is RetryAction.TERMINAL
    assert already_used.action is RetryAction.TERMINAL


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.NETWORK_ERROR,
        ErrorCode.RATE_LIMITED,
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.ADAPTER_UNSUPPORTED,
        ErrorCode.EXTRACTOR_BROKEN,
    ],
)
def test_fallback_adapter_has_exactly_one_attempt(
    policy: RetryPolicy,
    code: ErrorCode,
) -> None:
    decision = policy.decide(
        code,
        RetryContext(
            total_attempts=2,
            attempts_on_current_adapter=1,
            fallback_available=True,
            fallback_already_used=True,
        ),
    )
    assert decision.action is RetryAction.TERMINAL
    assert decision.reason == "fallback_attempt_already_used"


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.STORAGE_ERROR,
        ErrorCode.AUTHENTICATION_REQUIRED,
        ErrorCode.PRIVATE_CONTENT,
        ErrorCode.CONTENT_UNAVAILABLE,
        ErrorCode.GEO_RESTRICTED,
        ErrorCode.DRM_PROTECTED,
        ErrorCode.EGRESS_POLICY_BLOCKED,
        ErrorCode.INVALID_URL,
        ErrorCode.UNSUPPORTED_PLATFORM,
        ErrorCode.UNSUPPORTED_LINK_TYPE,
        ErrorCode.SHORT_LINK_RESOLUTION_REQUIRED,
    ],
)
def test_non_retryable_errors_are_terminal(
    policy: RetryPolicy,
    code: ErrorCode,
) -> None:
    decision = policy.decide(
        code,
        RetryContext(1, 1, fallback_available=True),
    )
    assert decision.action is RetryAction.TERMINAL


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.NETWORK_ERROR,
        ErrorCode.RATE_LIMITED,
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.ADAPTER_UNSUPPORTED,
        ErrorCode.EXTRACTOR_BROKEN,
    ],
)
def test_total_attempt_hard_limit_overrides_retry_and_fallback(
    policy: RetryPolicy,
    code: ErrorCode,
) -> None:
    decision = policy.decide(
        code,
        RetryContext(
            total_attempts=RetryPolicy.MAX_TOTAL_ATTEMPTS,
            attempts_on_current_adapter=1,
            fallback_available=True,
        ),
    )
    assert decision.action is RetryAction.TERMINAL
    assert decision.reason == "total_attempt_limit_reached"


def test_function_api_uses_the_same_policy() -> None:
    decision = decide_retry(
        ErrorCode.EXTRACTOR_BROKEN,
        total_attempts=3,
        attempts_on_current_adapter=3,
        fallback_available=True,
    )
    assert decision.action is RetryAction.FALLBACK


def test_context_rejects_impossible_counters() -> None:
    with pytest.raises(ValueError, match="failed attempt"):
        RetryContext(0, 0)
    with pytest.raises(ValueError, match="cannot exceed"):
        RetryContext(1, 2)
    with pytest.raises(ValueError, match="non-negative"):
        RetryContext(1, 1, retry_after_seconds=-1)
