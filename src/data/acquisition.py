"""Cache-first orchestration for canonical daily market-data acquisition."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast

import pandas as pd

from src.data.calendars import MarketCalendar, group_contiguous_sessions
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
    ActionCoverage,
    AttemptEvidence,
    CacheError,
    CacheEvidence,
    CacheStatus,
    ContractViolationError,
    DataAcquisitionError,
    InvalidRequestError,
    LineageSegment,
    NoUsableDataError,
    Provider,
    ProviderBatch,
    QualityFinding,
    QualityPolicy,
    QualitySeverity,
    RetryPolicy,
    SourcePreference,
)
from src.data.manifest import ManifestRepository
from src.data.normalization import CANONICAL_COLUMNS, normalize_provider_batch
from src.data.providers.base import ProviderEligibility
from src.data.quality import action_signature, evaluate_complete_request, evaluate_range_candidate
from src.data.retry import FailureClassification, RetryExecutor
from src.data.store import CacheReadResult, DataStore, GenerationPublication


class AcquisitionProvider(Protocol):
    @property
    def capabilities(self) -> Any: ...

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility: ...

    def fetch(self, request: AcquisitionRequest) -> ProviderBatch: ...


ProviderFactory = Callable[[], AcquisitionProvider]


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    recent_ttl: timedelta = timedelta(hours=1)
    historical_ttl: timedelta = timedelta(days=7)
    availability_lag: timedelta = timedelta(minutes=30)
    recent_overlap_sessions: int = 5

    def __post_init__(self) -> None:
        if (
            self.recent_ttl < timedelta(0)
            or self.historical_ttl < timedelta(0)
            or self.availability_lag < timedelta(0)
            or self.recent_overlap_sessions < 1
        ):
            raise ValueError("freshness durations must be non-negative and overlap positive")


@dataclass(frozen=True, slots=True)
class _AcceptedRange:
    request: AcquisitionRequest
    frame: pd.DataFrame
    provider: Provider
    acquired_at: datetime
    action_coverage: ActionCoverage
    has_warnings: bool


@dataclass(slots=True)
class _Evidence:
    attempts: list[AttemptEvidence]
    findings: list[QualityFinding]
    rejected_rows: list[Any]
    provider_skips: dict[str, str]


class AcquisitionService:
    """Acquire, validate, merge, and publish one admitted daily request."""

    def __init__(
        self,
        *,
        store: DataStore,
        manifest_repository: ManifestRepository,
        calendar: MarketCalendar,
        provider_factories: Mapping[Provider, ProviderFactory],
        retry_executor: RetryExecutor,
        clock: Callable[[], datetime],
        quality_policy: QualityPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        environment_versions: Mapping[str, str] | None = None,
    ) -> None:
        if any(not isinstance(provider, Provider) for provider in provider_factories):
            raise ValueError("provider registry contains an unsupported provider")
        self._store = store
        self._manifest_repository = manifest_repository
        self._calendar = calendar
        self._provider_factories = dict(provider_factories)
        self._retry = retry_executor
        self._clock = clock
        self._quality_policy = quality_policy or QualityPolicy()
        self._retry_policy = retry_policy or RetryPolicy()
        self._freshness = freshness_policy or FreshnessPolicy()
        self._environment_versions = dict(environment_versions or calendar.version_evidence())

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        """Return a complete usable result; archive admitted failures before raising."""
        if not isinstance(request, AcquisitionRequest):
            raise InvalidRequestError("acquisition requires a validated AcquisitionRequest")
        admission = self._manifest_repository.admit(
            symbol=request.symbol,
            start=request.start,
            end=request.end,
            interval=request.interval,
            calendar=request.calendar,
            source=SourcePreference(request.source).value,
            use_cache=request.use_cache,
            refresh=request.refresh,
        )
        started_at = admission.admitted_at
        evidence = _Evidence([], [], [], {})
        cache = CacheReadResult(CacheStatus.MISS)
        cache_status = CacheStatus.MISS
        try:
            now = self._utc_now()
            expected = self._expected_sessions(request)
            cache = self._read_cache(request)
            cached_frame = self._cache_frame(cache)
            requested_cached = self._requested_frame(cached_frame, expected)
            base_generation = cache.generation_id
            cache_status, planned = self._plan_ranges(
                request, expected, cache, requested_cached, now
            )
        except Exception as error:
            self._archive_failure(
                admission.acquisition_id,
                request,
                started_at,
                cache_status,
                cache,
                evidence,
                error,
            )
            raise

        if not planned:
            final = evaluate_complete_request(
                requested_cached,
                request,
                expected,
                self._quality_policy,
            )
            if final.is_fatal or final.frame is None:
                full_cache_error = NoUsableDataError(
                    "fresh cache does not satisfy complete quality policy"
                )
                self._archive_failure(
                    admission.acquisition_id,
                    request,
                    started_at,
                    cache_status,
                    cache,
                    evidence,
                    full_cache_error,
                )
                raise full_cache_error
            manifest = self._manifest(
                admission.acquisition_id,
                request,
                AcquisitionStatus.PARTIAL_SUCCESS
                if final.severity is QualitySeverity.WARNING
                else AcquisitionStatus.SUCCESS,
                started_at,
                cache_status,
                cache,
                final,
                evidence,
                self._cached_lineage(cache),
                output_hash=self._cached_output_hash(cache),
            )
            self._manifest_repository.archive(manifest)
            return AcquisitionResult(final.frame, manifest)

        providers: dict[Provider, AcquisitionProvider] = {}
        try:
            accepted = [
                self._fetch_range(request, start, end, providers, evidence)
                for start, end in planned
            ]
            if self._actions_changed(requested_cached, accepted) and not self._covers_all(
                expected, planned
            ):
                accepted = [
                    self._fetch_range(
                        request,
                        expected[0],
                        expected[-1],
                        providers,
                        evidence,
                    )
                ]
                planned = ((expected[0], expected[-1]),)
            assembled = self._merge(cached_frame, accepted)
            requested = self._requested_frame(assembled, expected)
            final = evaluate_complete_request(requested, request, expected, self._quality_policy)
            evidence.findings.extend(final.findings)
            if final.is_fatal or final.frame is None:
                raise NoUsableDataError("complete merged request failed quality policy")
            lineage = self._rebuild_lineage(assembled, cache, accepted)
            status = (
                AcquisitionStatus.PARTIAL_SUCCESS
                if self._accepted_warnings(accepted, final)
                else AcquisitionStatus.SUCCESS
            )
            manifest = self._manifest(
                admission.acquisition_id,
                request,
                status,
                started_at,
                cache_status,
                cache,
                final,
                evidence,
                lineage,
            )
            publication = self._store.publish_generation(
                request,
                assembled,
                {
                    "requested_start": request.start.isoformat(),
                    "requested_end": request.end.isoformat(),
                    "planned_ranges": [
                        {"start": start.date().isoformat(), "end": end.date().isoformat()}
                        for start, end in planned
                    ],
                },
                manifest,
                base_generation_id=base_generation,
                revalidate=lambda candidate: self._revalidate_rebase(candidate, request, expected),
                replace_ranges=tuple((start.date(), end.date()) for start, end in planned),
            )
            self._archive_publication_if_needed(publication)
            result_frame = self._requested_frame(publication.frame, expected)
            return AcquisitionResult(result_frame, publication.manifest)
        except Exception as error:
            self._archive_failure(
                admission.acquisition_id, request, started_at, cache_status, cache, evidence, error
            )
            if isinstance(error, (InvalidRequestError, ContractViolationError, CacheError)):
                raise
            if isinstance(error, NoUsableDataError):
                raise
            if self._retry.classify(error) is FailureClassification.TERMINAL:
                raise
            raise NoUsableDataError("no provider supplied a complete usable result") from error

    def _read_cache(self, request: AcquisitionRequest) -> CacheReadResult:
        if not request.use_cache:
            return CacheReadResult(CacheStatus.MISS)
        return self._store.read_generation(request)

    @staticmethod
    def _cache_frame(cache: CacheReadResult) -> pd.DataFrame:
        return _empty_canonical() if cache.frame is None else cache.frame.copy(deep=True)

    def _plan_ranges(
        self,
        request: AcquisitionRequest,
        expected: pd.DatetimeIndex,
        cache: CacheReadResult,
        requested_cached: pd.DataFrame,
        now: datetime,
    ) -> tuple[CacheStatus, tuple[tuple[pd.Timestamp, pd.Timestamp], ...]]:
        if not request.use_cache:
            return CacheStatus.FORCED_REFRESH, group_contiguous_sessions(expected, expected)
        if cache.status is CacheStatus.INVALIDATED:
            return CacheStatus.INVALIDATED, group_contiguous_sessions(expected, expected)

        cached_sessions = pd.DatetimeIndex(requested_cached["timestamp"])
        missing = expected.difference(cached_sessions)
        refresh_sessions = pd.DatetimeIndex([])
        if request.refresh:
            refresh_sessions = expected
            status = CacheStatus.FORCED_REFRESH
        else:
            stale = self._stale_sessions(request, expected, cache, now)
            refresh_sessions = stale
            if len(stale):
                status = CacheStatus.STALE_REFRESH
            elif len(missing):
                status = CacheStatus.MISS if cached_sessions.empty else CacheStatus.PARTIAL_HIT
            else:
                status = CacheStatus.FULL_HIT
        needed = missing.union(refresh_sessions).sort_values()
        return status, group_contiguous_sessions(expected, needed)

    def _stale_sessions(
        self,
        request: AcquisitionRequest,
        expected: pd.DatetimeIndex,
        cache: CacheReadResult,
        now: datetime,
    ) -> pd.DatetimeIndex:
        lineage = self._cached_lineage(cache)
        if cache.frame is None or cache.frame.empty:
            return pd.DatetimeIndex([])
        if not lineage:
            return expected
        latest = self._latest_completed(request, now)
        stale = pd.DatetimeIndex([])
        recent_window = pd.DatetimeIndex([])
        if latest is not None:
            position = expected.get_indexer(pd.DatetimeIndex([latest]))[0]
            first = max(0, position - self._freshness.recent_overlap_sessions + 1)
            recent_window = expected[first : position + 1]
        covered_by_lineage = pd.DatetimeIndex([])
        for segment in lineage:
            relevant = expected[
                (expected >= pd.Timestamp(segment.start)) & (expected <= pd.Timestamp(segment.end))
            ]
            if relevant.empty:
                continue
            covered_by_lineage = covered_by_lineage.union(relevant)
            age = now - _as_utc(segment.acquired_at)
            recent = relevant.intersection(recent_window)
            historical = relevant.difference(recent_window)
            if len(recent) and age > self._freshness.recent_ttl:
                stale = stale.union(recent)
            if len(historical) and age > self._freshness.historical_ttl:
                stale = stale.union(historical)
        cached_sessions = pd.DatetimeIndex(cache.frame["timestamp"])
        stale = stale.union(expected.intersection(cached_sessions).difference(covered_by_lineage))
        return stale.sort_values()

    def _latest_completed(self, request: AcquisitionRequest, now: datetime) -> pd.Timestamp | None:
        closes = self._calendar.session_closes(request.start, request.end)
        completed = [
            pd.Timestamp(session).tz_localize(None).normalize()
            for session, close in closes.items()
            if _as_utc(close) + self._freshness.availability_lag <= now
        ]
        return max(completed) if completed else None

    def _fetch_range(
        self,
        parent: AcquisitionRequest,
        start: pd.Timestamp,
        end: pd.Timestamp,
        providers: dict[Provider, AcquisitionProvider],
        evidence: _Evidence,
    ) -> _AcceptedRange:
        request = replace(
            parent, start=start.date(), end=end.date(), use_cache=False, refresh=False
        )
        expected = self._calendar.expected_sessions(request.start, request.end)
        errors: list[Exception] = []
        for provider_id in self._provider_order(parent.source):
            factory = self._provider_factories.get(provider_id)
            if factory is None:
                evidence.provider_skips.setdefault(provider_id.value, "provider is not configured")
                continue
            provider = providers.get(provider_id)
            if provider is None:
                provider = factory()
                providers[provider_id] = provider
            eligibility = provider.eligibility(request)
            if not eligibility.eligible:
                evidence.provider_skips[provider_id.value] = (
                    eligibility.reason or "provider is ineligible"
                )
                continue
            try:
                observed_execute = getattr(self._retry, "execute_observed", None)
                if callable(observed_execute):
                    retried = observed_execute(
                        provider_id,
                        lambda: provider.fetch(request),
                        evidence.attempts.append,
                    )
                else:
                    retried = self._retry.execute(provider_id, lambda: provider.fetch(request))
                    evidence.attempts.extend(retried.attempts)
                batch = retried.value
            except (InvalidRequestError, ContractViolationError):
                raise
            except Exception as error:
                if self._retry.classify(error) is FailureClassification.TERMINAL:
                    raise
                errors.append(error)
                continue
            if batch.provider is not provider_id or batch.request != request:
                raise ContractViolationError("provider batch identity is incompatible")
            quality = evaluate_range_candidate(
                normalize_provider_batch(batch), expected, self._quality_policy
            )
            evidence.findings.extend(quality.findings)
            evidence.rejected_rows.extend(quality.rejected_rows)
            if quality.is_fatal or quality.frame is None:
                errors.append(NoUsableDataError("provider range failed structural validation"))
                continue
            return _AcceptedRange(
                request,
                quality.frame,
                provider_id,
                _as_utc(batch.received_at),
                batch.action_coverage,
                quality.severity is QualitySeverity.WARNING,
            )
        detail = type(errors[-1]).__name__ if errors else "NoEligibleProvider"
        raise NoUsableDataError(f"provider candidates exhausted ({detail})")

    @staticmethod
    def _provider_order(source: SourcePreference | str) -> tuple[Provider, ...]:
        selected = SourcePreference(source)
        if selected is SourcePreference.AUTO:
            return (Provider.YFINANCE, Provider.ALPHA_VANTAGE)
        return (Provider(selected.value),)

    @staticmethod
    def _merge(cached: pd.DataFrame, accepted: list[_AcceptedRange]) -> pd.DataFrame:
        parts = [cached.copy(deep=True), *(item.frame.copy(deep=True) for item in accepted)]
        merged = pd.concat(parts, ignore_index=True)
        merged = merged.drop_duplicates("timestamp", keep="last")
        return merged.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    @staticmethod
    def _requested_frame(frame: pd.DataFrame, expected: pd.DatetimeIndex) -> pd.DataFrame:
        selected = frame[frame["timestamp"].isin(expected)].copy(deep=True)
        return selected.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    def _actions_changed(self, cached: pd.DataFrame, accepted: list[_AcceptedRange]) -> bool:
        if cached.empty:
            return False
        refreshed = pd.concat([item.frame for item in accepted], ignore_index=True)
        overlap = cached.merge(refreshed, on="timestamp", suffixes=("_old", "_new"))
        for _, row in overlap.iterrows():
            for column in ("dividend_amount", "split_coefficient"):
                if not math.isclose(
                    float(row[f"{column}_old"]),
                    float(row[f"{column}_new"]),
                    rel_tol=self._quality_policy.action_relative_tolerance,
                    abs_tol=self._quality_policy.action_absolute_tolerance,
                ):
                    return True
        return False

    @staticmethod
    def _covers_all(
        expected: pd.DatetimeIndex,
        ranges: tuple[tuple[pd.Timestamp, pd.Timestamp], ...],
    ) -> bool:
        return len(ranges) == 1 and ranges[0] == (expected[0], expected[-1])

    def _rebuild_lineage(
        self,
        assembled: pd.DataFrame,
        cache: CacheReadResult,
        accepted: list[_AcceptedRange],
    ) -> tuple[LineageSegment, ...]:
        cached_lineage = self._cached_lineage(cache)
        refreshed: dict[pd.Timestamp, _AcceptedRange] = {
            pd.Timestamp(timestamp): item
            for item in accepted
            for timestamp in item.frame["timestamp"]
        }
        default_time = self._cached_completed_at(cache) or self._utc_now()
        provenance: list[tuple[Provider, datetime, ActionCoverage, date, date, str]] = []
        for _, row in assembled.iterrows():
            timestamp = pd.Timestamp(row["timestamp"])
            new = refreshed.get(timestamp)
            if new is not None:
                provenance.append(
                    (
                        new.provider,
                        new.acquired_at,
                        new.action_coverage,
                        new.request.start,
                        new.request.end,
                        _frame_hash(new.frame),
                    )
                )
                continue
            provider = Provider(str(row["source"]))
            segment = next(
                (
                    item
                    for item in cached_lineage
                    if item.start <= timestamp.date() <= item.end and item.provider is provider
                ),
                None,
            )
            if segment is None:
                provenance.append(
                    (
                        provider,
                        default_time,
                        ActionCoverage.UNKNOWN,
                        timestamp.date(),
                        timestamp.date(),
                        "unsegmented",
                    )
                )
            else:
                provenance.append(
                    (
                        segment.provider,
                        segment.acquired_at,
                        segment.action_coverage,
                        segment.start,
                        segment.end,
                        segment.content_hash,
                    )
                )

        segments: list[LineageSegment] = []
        group_start = 0
        for index in range(1, len(assembled) + 1):
            if index < len(assembled) and provenance[index] == provenance[group_start]:
                continue
            group = assembled.iloc[group_start:index].copy(deep=True).reset_index(drop=True)
            provider, acquired_at, action_coverage, _, _, _ = provenance[group_start]
            segments.append(
                LineageSegment(
                    pd.Timestamp(group.iloc[0]["timestamp"]).date(),
                    pd.Timestamp(group.iloc[-1]["timestamp"]).date(),
                    provider,
                    acquired_at,
                    action_coverage,
                    _frame_hash(group),
                    action_signature(group),
                )
            )
            group_start = index
        return tuple(segments)

    @staticmethod
    def _cached_lineage(cache: CacheReadResult) -> tuple[LineageSegment, ...]:
        if cache.manifest is None:
            return ()
        value = cache.manifest.get("lineage", [])
        if not isinstance(value, list):
            return ()
        result: list[LineageSegment] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            try:
                result.append(
                    LineageSegment(
                        date.fromisoformat(cast(str, item["start"])),
                        date.fromisoformat(cast(str, item["end"])),
                        Provider(item["provider"]),
                        datetime.fromisoformat(cast(str, item["acquired_at"])),
                        ActionCoverage(item["action_coverage"]),
                        cast(str, item["content_hash"]),
                        cast(str, item["action_signature"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return ()
        return tuple(result)

    def _manifest(
        self,
        acquisition_id: str,
        request: AcquisitionRequest,
        status: AcquisitionStatus,
        started_at: datetime,
        cache_status: CacheStatus,
        cache: CacheReadResult,
        final: Any,
        evidence: _Evidence,
        lineage: tuple[LineageSegment, ...],
        *,
        output_hash: str | None = None,
    ) -> AcquisitionManifest:
        completed = self._utc_now()
        covered_sessions, missing_sessions = self._cache_counts(cache, request)
        return AcquisitionManifest(
            acquisition_id,
            request,
            status,
            quality_policy=self._quality_policy,
            retry_policy=self._retry_policy,
            environment_versions=self._environment_versions,
            cache=CacheEvidence(
                cache_status,
                generation_id=cache.generation_id,
                compatibility_reason=cache.reason,
                covered_sessions=covered_sessions,
                missing_sessions=missing_sessions,
            ),
            attempts=tuple(evidence.attempts),
            provider_skips=evidence.provider_skips,
            findings=tuple(evidence.findings),
            rejected_rows=tuple(evidence.rejected_rows),
            lineage=lineage,
            counters=asdict(final.counters),
            coverage=final.coverage,
            output_hash=output_hash,
            duration_seconds=max(0.0, (completed - started_at).total_seconds()),
            started_at=started_at,
            completed_at=completed,
        )

    def _archive_failure(
        self,
        acquisition_id: str,
        request: AcquisitionRequest,
        started_at: datetime,
        cache_status: CacheStatus,
        cache: CacheReadResult,
        evidence: _Evidence,
        error: Exception,
    ) -> None:
        completed = self._utc_now()
        covered_sessions, missing_sessions = self._cache_counts(cache, request)
        findings = tuple(evidence.findings) + (
            QualityFinding(
                QualitySeverity.FATAL,
                "acquisition_failed",
                "acquisition did not produce a complete usable result",
                {"error_type": type(error).__name__},
            ),
        )
        manifest = AcquisitionManifest(
            acquisition_id,
            request,
            AcquisitionStatus.FAILED,
            quality_policy=self._quality_policy,
            retry_policy=self._retry_policy,
            environment_versions=self._environment_versions,
            cache=CacheEvidence(
                cache_status,
                generation_id=cache.generation_id,
                compatibility_reason=cache.reason,
                covered_sessions=covered_sessions,
                missing_sessions=missing_sessions,
            ),
            attempts=tuple(evidence.attempts),
            provider_skips=evidence.provider_skips,
            findings=findings,
            rejected_rows=tuple(evidence.rejected_rows),
            duration_seconds=max(0.0, (completed - started_at).total_seconds()),
            started_at=started_at,
            completed_at=completed,
        )
        self._manifest_repository.archive(manifest)

    def _archive_publication_if_needed(self, publication: GenerationPublication) -> None:
        if not self._store.archives_publications:
            self._manifest_repository.archive(publication.manifest)

    def _revalidate_rebase(
        self,
        candidate: pd.DataFrame,
        request: AcquisitionRequest,
        expected: pd.DatetimeIndex,
    ) -> bool:
        requested = self._requested_frame(candidate, expected)
        return not evaluate_complete_request(
            requested, request, expected, self._quality_policy
        ).is_fatal

    def _expected_sessions(self, request: AcquisitionRequest) -> pd.DatetimeIndex:
        sessions = pd.DatetimeIndex(self._calendar.expected_sessions(request.start, request.end))
        if sessions.tz is not None:
            sessions = sessions.tz_localize(None)
        return sessions.normalize().sort_values()

    @staticmethod
    def _accepted_warnings(accepted: list[_AcceptedRange], final: Any) -> bool:
        return final.severity is QualitySeverity.WARNING or any(
            item.has_warnings for item in accepted
        )

    @staticmethod
    def _cached_output_hash(cache: CacheReadResult) -> str | None:
        value = None if cache.manifest is None else cache.manifest.get("output_hash")
        return value if isinstance(value, str) else None

    @staticmethod
    def _cached_completed_at(cache: CacheReadResult) -> datetime | None:
        value = None if cache.manifest is None else cache.manifest.get("completed_at")
        if not isinstance(value, str):
            return None
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None

    def _cache_counts(
        self,
        cache: CacheReadResult,
        request: AcquisitionRequest,
    ) -> tuple[int, int]:
        try:
            expected = self._expected_sessions(request)
            covered = len(self._requested_frame(self._cache_frame(cache), expected))
            return covered, len(expected) - covered
        except DataAcquisitionError:
            return 0, 0

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ContractViolationError("clock must return a datetime")
        return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _empty_canonical() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns]"),
            "symbol": pd.Series(dtype="string"),
            **{column: pd.Series(dtype="float64") for column in CANONICAL_COLUMNS[2:-1]},
            "source": pd.Series(dtype="string"),
        }
    ).loc[:, list(CANONICAL_COLUMNS)]


def _frame_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_json(orient="records", date_format="iso", double_precision=15)
    return hashlib.sha256(payload.encode()).hexdigest()
