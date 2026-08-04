"""Provider protocols and capability-aware eligibility planning.

Provider adapters perform exactly one transport operation followed by provider
response parsing.  They deliberately do not retry or normalize provider-native
frames; those concerns belong to the acquisition and retry layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.data.contracts import AcquisitionRequest, ProviderCapabilities


@dataclass(frozen=True, slots=True)
class ProviderEligibility:
    """Whether a configured adapter can serve one request without I/O."""

    eligible: bool
    reason: str | None = None


class MarketDataProvider(Protocol):
    """The small adapter surface used by capability planning."""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility: ...


@dataclass(frozen=True, slots=True)
class ProviderCandidatePlan:
    """A deterministic partition of eligible and skipped provider adapters."""

    eligible: tuple[MarketDataProvider, ...]
    skipped: tuple[ProviderEligibility, ...]


def plan_provider_candidates(
    providers: tuple[MarketDataProvider, ...], request: AcquisitionRequest
) -> ProviderCandidatePlan:
    """Plan candidates without constructing requests or performing network I/O."""
    eligible: list[MarketDataProvider] = []
    skipped: list[ProviderEligibility] = []
    for provider in providers:
        eligibility = provider.eligibility(request)
        if eligibility.eligible:
            eligible.append(provider)
        else:
            skipped.append(eligibility)
    return ProviderCandidatePlan(tuple(eligible), tuple(skipped))
