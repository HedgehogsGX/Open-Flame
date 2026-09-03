"""Deterministic retry/fallback policy for download attempts.

The policy is intentionally pure: it reads only the supplied failure/context
and returns a decision.  It does not sleep, mutate a job, inspect the adapter
registry, or enqueue work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import ErrorCode


class RetryAction(StrEnum):
    RETRY = "retry"
    FALLBACK = "fallback"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RetryContext:
    """Attempt counters after the current attempt has failed.

    ``total_attempts`` and ``attempts_on_current_adapter`` both include the
    just-failed attempt.  A decision to retry/fallback therefore consumes one
    additional attempt when the Worker later executes it.
    """

    total_attempts: int
    attempts_on_current_adapter: int
    fallback_available: bool = False
    fallback_already_used: bool = False
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.total_attempts < 1:
            raise ValueError("total_attempts must include the failed attempt")
        if self.attempts_on_current_adapter < 1:
            raise ValueError(
                "attempts_on_current_adapter must include the failed attempt"
            )
        if self.attempts_on_current_adapter > self.total_attempts:
            raise ValueError(
                "attempts_on_current_adapter cannot exceed total_attempts"
            )
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    action: RetryAction
    delay_seconds: float
    reason: str

    @property
    def is_terminal(self) -> bool:
        return self.action is RetryAction.TERMINAL


class RetryPolicy:
    """Implements specification section 5.4.1's error matrix."""

    MAX_TOTAL_ATTEMPTS = 4

    _FALLBACK_ERRORS = frozenset(
        {
            ErrorCode.ADAPTER_UNSUPPORTED,
            ErrorCode.EXTRACTOR_BROKEN,
        }
    )
    _MAX_SAME_ADAPTER_RETRIES = {
        ErrorCode.NETWORK_ERROR: 2,
        ErrorCode.RATE_LIMITED: 2,
        ErrorCode.VALIDATION_FAILED: 1,
    }
    _BASE_DELAYS_SECONDS = {
        ErrorCode.NETWORK_ERROR: 5.0,
        ErrorCode.RATE_LIMITED: 60.0,
        ErrorCode.VALIDATION_FAILED: 0.0,
    }

    def decide(
        self,
        error_code: ErrorCode,
        context: RetryContext,
    ) -> RetryDecision:
        """Return a decision without causing any side effects."""

        if context.total_attempts >= self.MAX_TOTAL_ATTEMPTS:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                delay_seconds=0.0,
                reason="total_attempt_limit_reached",
            )

        # The specification permits a configured fallback adapter to be called
        # once, not to start a second retry budget on that adapter.
        if context.fallback_already_used:
            return RetryDecision(
                action=RetryAction.TERMINAL,
                delay_seconds=0.0,
                reason="fallback_attempt_already_used",
            )

        if error_code in self._FALLBACK_ERRORS:
            if context.fallback_available:
                return RetryDecision(
                    action=RetryAction.FALLBACK,
                    delay_seconds=0.0,
                    reason="explicit_fallback_available",
                )
            return RetryDecision(
                action=RetryAction.TERMINAL,
                delay_seconds=0.0,
                reason="fallback_unavailable_or_already_used",
            )

        retry_limit = self._MAX_SAME_ADAPTER_RETRIES.get(error_code)
        if retry_limit is not None:
            retries_already_used = context.attempts_on_current_adapter - 1
            if retries_already_used < retry_limit:
                policy_delay = self._backoff_seconds(
                    error_code,
                    context.attempts_on_current_adapter,
                )
                requested_delay = context.retry_after_seconds or 0.0
                return RetryDecision(
                    action=RetryAction.RETRY,
                    delay_seconds=max(policy_delay, requested_delay),
                    reason="same_adapter_retry_available",
                )
            return RetryDecision(
                action=RetryAction.TERMINAL,
                delay_seconds=0.0,
                reason="same_adapter_retry_limit_reached",
            )

        return RetryDecision(
            action=RetryAction.TERMINAL,
            delay_seconds=0.0,
            reason="terminal_error",
        )

    def _backoff_seconds(
        self,
        error_code: ErrorCode,
        attempts_on_current_adapter: int,
    ) -> float:
        base = self._BASE_DELAYS_SECONDS[error_code]
        return base * (2 ** (attempts_on_current_adapter - 1))


def decide_retry(
    error_code: ErrorCode,
    *,
    total_attempts: int,
    attempts_on_current_adapter: int,
    fallback_available: bool = False,
    fallback_already_used: bool = False,
    retry_after_seconds: float | None = None,
) -> RetryDecision:
    """Convenience pure function for callers that do not retain a policy."""

    return RetryPolicy().decide(
        error_code,
        RetryContext(
            total_attempts=total_attempts,
            attempts_on_current_adapter=attempts_on_current_adapter,
            fallback_available=fallback_available,
            fallback_already_used=fallback_already_used,
            retry_after_seconds=retry_after_seconds,
        ),
    )
