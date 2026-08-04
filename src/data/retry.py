"""Injected retry execution with explicit typed failure classification."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar

from src.data.contracts import (
    AttemptEvidence,
    DataAcquisitionError,
    Provider,
    ProviderEntitlementError,
    ProviderQuotaError,
    RetryPolicy,
    TransientProviderError,
)

T = TypeVar("T")


class FailureClassification(StrEnum):
    RETRYABLE = "retryable"
    FALLBACKABLE = "fallbackable"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class RetryResult(Generic[T]):
    value: T
    attempts: tuple[AttemptEvidence, ...]


class RetryExecutor:
    """Retry only transient typed errors using injected time and randomness."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        max_retry_after_seconds: float | None = None,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper
        self._random_source = random_source
        self._max_retry_after_seconds = (
            self._policy.max_retry_after_seconds
            if max_retry_after_seconds is None
            else max_retry_after_seconds
        )
        if self._max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be non-negative")

    @staticmethod
    def classify(error: Exception) -> FailureClassification:
        if isinstance(error, TransientProviderError):
            return FailureClassification.RETRYABLE
        if isinstance(error, (ProviderQuotaError, ProviderEntitlementError)):
            return FailureClassification.FALLBACKABLE
        return FailureClassification.TERMINAL

    def execute(self, provider: Provider, operation: Callable[[], T]) -> RetryResult[T]:
        """Run an operation and return immutable evidence when it succeeds."""
        return self._execute(provider, operation, lambda _attempt: None)

    def execute_observed(
        self,
        provider: Provider,
        operation: Callable[[], T],
        observer: Callable[[AttemptEvidence], None],
    ) -> RetryResult[T]:
        """Run with an injected immutable-attempt observer, including terminal failures."""
        return self._execute(provider, operation, observer)

    def _execute(
        self,
        provider: Provider,
        operation: Callable[[], T],
        observer: Callable[[AttemptEvidence], None],
    ) -> RetryResult[T]:
        attempts: list[AttemptEvidence] = []
        for number in range(1, self._policy.max_attempts + 1):
            started_at = self._clock()
            try:
                value = operation()
            except Exception as error:
                duration = max(0.0, (self._clock() - started_at).total_seconds())
                classification = self.classify(error)
                if (
                    classification is not FailureClassification.RETRYABLE
                    or number == self._policy.max_attempts
                ):
                    observer(
                        AttemptEvidence(
                            provider=provider,
                            attempt_number=number,
                            started_at=started_at,
                            duration_seconds=duration,
                            outcome="failed",
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )
                    )
                    raise
                delay = self._delay_seconds(number, error)
                attempt = AttemptEvidence(
                    provider=provider,
                    attempt_number=number,
                    started_at=started_at,
                    duration_seconds=duration,
                    outcome="retry",
                    retry_delay_seconds=delay,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                attempts.append(attempt)
                observer(attempt)
                self._sleeper(delay)
                continue
            duration = max(0.0, (self._clock() - started_at).total_seconds())
            attempt = AttemptEvidence(
                provider=provider,
                attempt_number=number,
                started_at=started_at,
                duration_seconds=duration,
                outcome="success",
            )
            attempts.append(attempt)
            observer(attempt)
            return RetryResult(value=value, attempts=tuple(attempts))
        raise DataAcquisitionError("retry loop terminated unexpectedly")

    def _delay_seconds(self, attempt_number: int, error: Exception) -> float:
        retry_after: object = getattr(error, "retry_after_seconds", None)
        if isinstance(retry_after, int | float) and retry_after >= 0:
            return min(float(retry_after), self._max_retry_after_seconds)
        cap = min(
            self._policy.max_delay_seconds,
            self._policy.base_delay_seconds * (2 ** (attempt_number - 1)),
        )
        jitter = self._random_source()
        return float(cap * min(1.0, max(0.0, jitter)))
