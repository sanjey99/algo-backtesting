# ADR-0002: Capability-Aware Provider Fallback

**Date:** 2026-08-04
**Status:** accepted

## Context

yfinance and Alpha Vantage differ in date semantics, response shape, credentials, entitlements,
coverage, and quotas. Alpha Vantage currently documents adjusted daily data as premium, while the
repository's five-calls-per-minute constant is not a universal current limit.

## Decision

Use yfinance first for `auto`. Consider Alpha Vantage only when credentials and explicit adjusted-
daily entitlement are configured. Classify errors into retryable, fallbackable, or terminal types;
never infer policy from exception strings.

## Alternatives

- Blindly try both providers: wastes quota and misrepresents unavailable capabilities.
- Require callers to choose every source: predictable but provides no resilient default.
- Merge conflicting provider rows: hides provenance and creates an unjustified consensus price.

## Consequences

- Selection and failure behavior are explainable in every manifest.
- Fallback cannot silently bypass request, entitlement, or data-quality constraints.
- Alpha Vantage configuration must declare entitlement and quota policy.
- Process-local pacing is explicitly not a distributed rate limiter.
