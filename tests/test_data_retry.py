from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.data.contracts import (
    InvalidRequestError,
    Provider,
    ProviderEntitlementError,
    ProviderQuotaError,
    TransientProviderError,
)
from src.data.retry import FailureClassification, RetryExecutor


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2024, 1, 2, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_retry_retries_transient_failures_with_exact_jittered_delays_and_evidence() -> None:
    clock = _Clock()
    slept: list[float] = []
    outcomes = iter(
        [TransientProviderError("temporary one"), TransientProviderError("temporary two"), "ok"]
    )

    def operation() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sleep(delay: float) -> None:
        slept.append(delay)
        clock.advance(delay)

    executor = RetryExecutor(clock=clock, sleeper=sleep, random_source=iter([0.5, 0.25]).__next__)
    result = executor.execute(Provider.YFINANCE, operation)

    assert result.value == "ok"
    assert slept == [0.25, 0.25]
    assert [attempt.outcome for attempt in result.attempts] == ["retry", "retry", "success"]
    assert [attempt.retry_delay_seconds for attempt in result.attempts] == [0.25, 0.25, None]
    assert [attempt.attempt_number for attempt in result.attempts] == [1, 2, 3]


@pytest.mark.parametrize(
    "error",
    [
        InvalidRequestError("invalid"),
        ProviderQuotaError("quota"),
        ProviderEntitlementError("entitlement"),
    ],
)
def test_retry_does_not_retry_terminal_or_fallbackable_failures(error: Exception) -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)):
        RetryExecutor().execute(Provider.YFINANCE, operation)

    assert calls == 1


def test_retry_classifies_quota_and_entitlement_as_fallbackable() -> None:
    assert FailureClassification.FALLBACKABLE is RetryExecutor.classify(ProviderQuotaError("quota"))
    assert FailureClassification.FALLBACKABLE is RetryExecutor.classify(
        ProviderEntitlementError("entitlement")
    )
    assert FailureClassification.TERMINAL is RetryExecutor.classify(InvalidRequestError("invalid"))


def test_retry_after_delay_is_bounded_and_credentials_are_redacted() -> None:
    clock = _Clock()
    slept: list[float] = []
    error = TransientProviderError("request failed: apikey=super-secret-key")
    setattr(error, "retry_after_seconds", 100.0)
    outcomes = iter([error, "ok"])

    def operation() -> str:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result = RetryExecutor(
        clock=clock,
        sleeper=lambda delay: slept.append(delay),
        random_source=lambda: 1.0,
        max_retry_after_seconds=2.0,
    ).execute(Provider.ALPHA_VANTAGE, operation)

    assert slept == [2.0]
    assert "super-secret-key" not in str(error)
    assert "super-secret-key" not in str(result)
    assert "super-secret-key" not in str(result.attempts)
