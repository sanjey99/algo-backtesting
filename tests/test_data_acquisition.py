from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.data.acquisition import AcquisitionService
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    ActionCoverage,
    CacheEvidence,
    CacheStatus,
    ContractViolationError,
    InvalidRequestError,
    LineageSegment,
    NoUsableDataError,
    Provider,
    ProviderBatch,
    ProviderCapabilities,
    ProviderEntitlementError,
    ProviderSchemaError,
    TransientProviderError,
)
from src.data.manifest import ManifestRepository
from src.data.providers.base import ProviderEligibility
from src.data.retry import RetryExecutor
from src.data.store import DataStore

NOW = datetime(2024, 1, 10, 22, tzinfo=UTC)
SESSIONS = pd.DatetimeIndex(
    pd.to_datetime(
        [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-08",
            "2024-01-09",
            "2024-01-10",
        ]
    )
)


class FakeCalendar:
    calendar_id = "XNYS"

    def __init__(self, sessions: pd.DatetimeIndex = SESSIONS) -> None:
        self.sessions = sessions

    def expected_sessions(self, start: date | datetime, end: date | datetime) -> pd.DatetimeIndex:
        first = pd.Timestamp(start)
        last = pd.Timestamp(end)
        return self.sessions[(self.sessions >= first) & (self.sessions <= last)]

    def session_closes(
        self, start: date | datetime, end: date | datetime
    ) -> Mapping[pd.Timestamp, datetime]:
        return {
            session: datetime.combine(session.date(), datetime.min.time(), UTC)
            + timedelta(hours=21)
            for session in self.expected_sessions(start, end)
        }

    def version_evidence(self) -> Mapping[str, str]:
        return {"calendar": "XNYS", "calendar_version": "fake-1"}


class FailingCloseCalendar(FakeCalendar):
    def session_closes(
        self, start: date | datetime, end: date | datetime
    ) -> Mapping[pd.Timestamp, datetime]:
        raise ContractViolationError("close schedule unavailable")


class FakeProvider:
    def __init__(
        self,
        provider: Provider,
        batches: Callable[[AcquisitionRequest], ProviderBatch],
        *,
        eligible: bool = True,
        reason: str | None = None,
    ) -> None:
        self._provider = provider
        self._batches = batches
        self._eligible = eligible
        self._reason = reason
        self.requests: list[AcquisitionRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self._provider, supports_actions=True)

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility:
        return ProviderEligibility(self._eligible, self._reason)

    def fetch(self, request: AcquisitionRequest) -> ProviderBatch:
        self.requests.append(request)
        return self._batches(request)


def native_batch(
    provider: Provider,
    request: AcquisitionRequest,
    sessions: pd.DatetimeIndex | None = None,
    *,
    dividend: float = 0.0,
    bad_high: bool = False,
) -> ProviderBatch:
    selected = (
        sessions
        if sessions is not None
        else FakeCalendar().expected_sessions(request.start, request.end)
    )
    base = [
        10.0 + int(SESSIONS.get_indexer(pd.DatetimeIndex([session]))[0])
        for session in selected
    ]
    if provider is Provider.YFINANCE:
        frame = pd.DataFrame(
            {
                "Open": base,
                "High": [value - 1 if bad_high else value + 1 for value in base],
                "Low": [value - 1 for value in base],
                "Close": [value + 0.5 for value in base],
                "Volume": [100.0] * len(selected),
                "Adj Close": [value + 0.5 for value in base],
                "Dividends": [dividend] * len(selected),
                "Stock Splits": [1.0] * len(selected),
            },
            index=selected,
        )
    else:
        frame = pd.DataFrame(
            {
                "1. open": base,
                "2. high": [value + 1 for value in base],
                "3. low": [value - 1 for value in base],
                "4. close": [value + 0.5 for value in base],
                "5. adjusted close": [value + 0.5 for value in base],
                "6. volume": [100.0] * len(selected),
                "7. dividend amount": [dividend] * len(selected),
                "8. split coefficient": [1.0] * len(selected),
            },
            index=selected,
        )
    return ProviderBatch(
        provider,
        request,
        frame,
        received_at=NOW,
        raw_row_count=len(frame),
        action_coverage=ActionCoverage.REPRESENTED,
    )


def canonical_frame(request: AcquisitionRequest, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for index, session in enumerate(sessions):
        value = 10.0 + index
        rows.append(
            {
                "timestamp": session,
                "symbol": request.symbol,
                "open": value,
                "high": value + 1,
                "low": value - 1,
                "close": value + 0.5,
                "volume": 100.0,
                "adj_close": value + 0.5,
                "dividend_amount": 0.0,
                "split_coefficient": 1.0,
                "source": Provider.YFINANCE.value,
            }
        )
    frame = pd.DataFrame.from_records(rows)
    frame["timestamp"] = frame["timestamp"].astype("datetime64[ns]")
    frame["symbol"] = frame["symbol"].astype("string")
    for column in (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
        "dividend_amount",
        "split_coefficient",
    ):
        frame[column] = frame[column].astype("float64")
    frame["source"] = frame["source"].astype("string")
    return frame


def service(
    tmp_path: Path,
    factories: Mapping[Provider, Callable[[], FakeProvider]],
    *,
    now: datetime = NOW,
    calendar: FakeCalendar | None = None,
    repository: ManifestRepository | None = None,
    archive_in_store: bool = True,
) -> tuple[AcquisitionService, DataStore, ManifestRepository]:
    chosen_calendar = calendar or FakeCalendar()
    selected_repository = repository or ManifestRepository(
        tmp_path / "reports",
        id_factory=lambda: "acquisition-1",
        clock=lambda: now,
    )
    generation_number = 0

    def generation_id() -> str:
        nonlocal generation_number
        generation_number += 1
        return f"generation-{generation_number}"

    store = DataStore(
        tmp_path / "cache",
        calendar_versions=chosen_calendar.version_evidence(),
        generation_id_factory=generation_id,
        clock=lambda: now,
        manifest_repository=selected_repository if archive_in_store else None,
    )
    return (
        AcquisitionService(
            store=store,
            manifest_repository=selected_repository,
            calendar=chosen_calendar,
            provider_factories=factories,
            retry_executor=RetryExecutor(clock=lambda: now, sleeper=lambda _: None),
            clock=lambda: now,
        ),
        store,
        selected_repository,
    )


def test_full_fresh_hit_constructs_no_provider_and_archives_report(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    built = 0

    def factory() -> FakeProvider:
        nonlocal built
        built += 1
        return FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))

    acquisition, store, repository = service(tmp_path, {Provider.YFINANCE: factory})
    cached = canonical_frame(request, SESSIONS)
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(minutes=20),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            cache=CacheEvidence(CacheStatus.MISS),
            lineage=(lineage,),
            started_at=NOW - timedelta(minutes=20),
            completed_at=NOW - timedelta(minutes=20),
        ),
    )

    result = acquisition.acquire(request)

    assert built == 0
    assert result.manifest.cache.status is CacheStatus.FULL_HIT
    assert repository.lookup("acquisition-1") == result.manifest.to_dict()
    pd.testing.assert_frame_equal(result.frame, cached)


def test_partial_hit_groups_missing_exchange_sessions_and_publishes_once(tmp_path: Path) -> None:
    seed_request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 5))
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, store, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})
    seed_frame = canonical_frame(seed_request, SESSIONS[:4])
    lineage = LineageSegment(
        seed_request.start,
        seed_request.end,
        Provider.YFINANCE,
        NOW,
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        seed_request,
        seed_frame,
        {},
        AcquisitionManifest(
            "seed",
            seed_request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    result = acquisition.acquire(request)

    assert [(item.start, item.end) for item in provider.requests] == [
        (date(2024, 1, 8), date(2024, 1, 10))
    ]
    assert result.manifest.cache.status is CacheStatus.PARTIAL_HIT
    assert result.manifest.cache.covered_sessions == 4
    assert result.manifest.cache.missing_sessions == 3
    assert result.frame["timestamp"].tolist() == SESSIONS.tolist()
    assert result.frame.iloc[:4]["open"].tolist() == seed_frame["open"].tolist()
    assert [(item.start, item.end) for item in result.manifest.lineage] == [
        (date(2024, 1, 2), date(2024, 1, 5)),
        (date(2024, 1, 8), date(2024, 1, 10)),
    ]
    assert store.read_generation(request).generation_id != "generation-1" or len(result.frame) == 7


def test_one_session_range_is_structurally_accepted_before_final_coverage(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 10), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, _, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})

    result = acquisition.acquire(request)

    assert len(result.frame) == 1
    assert result.manifest.status is AcquisitionStatus.SUCCESS


def test_two_session_range_is_structurally_accepted_before_final_coverage(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, _, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})

    result = acquisition.acquire(request)

    assert len(result.frame) == 2
    assert result.manifest.coverage == 1.0


def test_warning_primary_is_accepted_but_fatal_primary_falls_back(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    warning_yf = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(
            Provider.YFINANCE,
            item,
            pd.DatetimeIndex(SESSIONS[-2:].append(pd.DatetimeIndex([SESSIONS[-1]]))),
        ),
    )
    acquisition, _, _ = service(
        tmp_path / "warning",
        {Provider.YFINANCE: lambda: warning_yf, Provider.ALPHA_VANTAGE: lambda: alpha},
    )
    warning_result = acquisition.acquire(request)
    assert warning_result.manifest.status is AcquisitionStatus.PARTIAL_SUCCESS
    assert alpha.requests == []

    fatal_yf = FakeProvider(
        Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item, bad_high=True)
    )
    alpha_fallback = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    fallback, _, _ = service(
        tmp_path / "fatal",
        {Provider.YFINANCE: lambda: fatal_yf, Provider.ALPHA_VANTAGE: lambda: alpha_fallback},
    )
    result = fallback.acquire(request)
    assert result.frame["source"].tolist() == [Provider.ALPHA_VANTAGE.value] * 2
    assert len(alpha_fallback.requests) == 1


def test_ineligible_alpha_is_skipped_and_all_source_failure_archives_without_publish(
    tmp_path: Path,
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda _: (_ for _ in ()).throw(TransientProviderError("down"))
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE,
        lambda item: native_batch(Provider.ALPHA_VANTAGE, item),
        eligible=False,
        reason="missing API key",
    )
    acquisition, store, repository = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    with pytest.raises(NoUsableDataError):
        acquisition.acquire(request)

    assert alpha.requests == []
    assert store.current_generation_id(request) is None
    report = repository.lookup("acquisition-1")
    assert report is not None
    assert report["status"] == "failed"
    assert report["provider_skips"] == {"alpha_vantage": "missing API key"}


def test_terminal_request_error_does_not_fall_back(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda _: (_ for _ in ()).throw(ProviderSchemaError("permanent"))
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    acquisition, _, repository = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    with pytest.raises(ProviderSchemaError):
        acquisition.acquire(request)

    assert alpha.requests == []
    report = repository.lookup("acquisition-1")
    assert report is not None
    assert report["status"] == "failed"


def test_cache_hit_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    acquisition, store, _ = service(tmp_path, {})
    cached = canonical_frame(request, SESSIONS)
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(minutes=20),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW - timedelta(minutes=20),
            completed_at=NOW - timedelta(minutes=20),
        ),
    )

    with caplog.at_level(logging.DEBUG):
        acquisition.acquire(request)

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "acquisition.cache_result"
    )
    fields = getattr(record, "event_fields", {})
    assert record.levelno == logging.DEBUG
    assert fields["cache_status"] == "full_hit"
    assert fields["planned_range_count"] == 0


def test_provider_failure_and_fallback_are_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    yfinance = FakeProvider(
        Provider.YFINANCE,
        lambda _: (_ for _ in ()).throw(TransientProviderError("down")),
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE,
        lambda request: native_batch(Provider.ALPHA_VANTAGE, request),
    )
    acquisition, _, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha},
    )

    with caplog.at_level(logging.DEBUG):
        acquisition.acquire(AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 10)))

    events = [getattr(record, "event", None) for record in caplog.records]
    assert "acquisition.provider_attempt" in events
    assert "acquisition.fallback" in events
    fallback = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "acquisition.fallback"
    )
    fields = getattr(fallback, "event_fields", {})
    assert fallback.levelno == logging.WARNING
    assert fields["provider"] == Provider.YFINANCE.value
    assert fields["exception_type"] == "TransientProviderError"
    assert all(
        "secret" not in str(getattr(record, "event_fields", {})).lower()
        for record in caplog.records
    )


def test_quality_warning_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(
            Provider.YFINANCE,
            item,
            pd.DatetimeIndex(SESSIONS[-2:].append(pd.DatetimeIndex([SESSIONS[-1]]))),
        ),
    )
    acquisition, _, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})

    with caplog.at_level(logging.WARNING):
        acquisition.acquire(request)

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "acquisition.quality_warning"
    )
    fields = getattr(record, "event_fields", {})
    assert record.levelno == logging.WARNING
    assert fields["provider"] == Provider.YFINANCE.value
    assert fields["severity"] == "warning"


def test_terminal_acquisition_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda _: (_ for _ in ()).throw(ProviderSchemaError("permanent")),
    )
    acquisition, _, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderSchemaError):
        acquisition.acquire(request)

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "acquisition.failed"
    )
    fields = getattr(record, "event_fields", {})
    assert record.levelno == logging.WARNING
    assert fields["exception_type"] == "ProviderSchemaError"


def test_typed_invalid_request_error_is_terminal_and_does_not_fall_back(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda _: (_ for _ in ()).throw(InvalidRequestError("terminal"))
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    acquisition, _, repository = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    with pytest.raises(InvalidRequestError):
        acquisition.acquire(request)

    assert alpha.requests == []
    assert repository.lookup("acquisition-1")["status"] == "failed"  # type: ignore[index]


def test_retry_exhaustion_falls_back_to_eligible_alpha(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda _: (_ for _ in ()).throw(TransientProviderError("temporary"))
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    acquisition, _, _ = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    result = acquisition.acquire(request)

    assert len(yfinance.requests) == 3
    assert len(alpha.requests) == 1
    assert [item.attempt_number for item in result.manifest.attempts] == [1, 2, 3, 1]
    assert [item.outcome for item in result.manifest.attempts] == [
        "retry",
        "retry",
        "failed",
        "success",
    ]
    assert set(result.frame["source"]) == {Provider.ALPHA_VANTAGE.value}


def test_failed_second_range_does_not_publish_first_range(tmp_path: Path) -> None:
    calendar = FakeCalendar(
        pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-05"]))
    )
    seed_request = AcquisitionRequest("AAPL", date(2024, 1, 3), date(2024, 1, 3))
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 5))

    def batches(item: AcquisitionRequest) -> ProviderBatch:
        if item.start == date(2024, 1, 5):
            raise ProviderEntitlementError("range failed")
        return native_batch(
            Provider.YFINANCE, item, calendar.expected_sessions(item.start, item.end)
        )

    provider = FakeProvider(Provider.YFINANCE, batches)
    acquisition, store, _ = service(
        tmp_path, {Provider.YFINANCE: lambda: provider}, calendar=calendar
    )
    seed_frame = canonical_frame(
        seed_request, calendar.expected_sessions(seed_request.start, seed_request.end)
    )
    lineage = LineageSegment(
        seed_request.start,
        seed_request.end,
        Provider.YFINANCE,
        NOW,
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        seed_request,
        seed_frame,
        {},
        AcquisitionManifest(
            "seed",
            seed_request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    with pytest.raises(NoUsableDataError):
        acquisition.acquire(request)

    assert store.current_generation_id(request) == "generation-1"


def test_recent_stale_refresh_overlaps_five_sessions_and_new_rows_win(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, store, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})
    cached = canonical_frame(request, SESSIONS)
    cached.loc[
        cached["timestamp"] == pd.Timestamp("2024-01-04"), ["open", "high", "close", "adj_close"]
    ] = [99.0, 101.0, 100.0, 100.0]
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(hours=2),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW - timedelta(hours=2),
            completed_at=NOW - timedelta(hours=2),
        ),
    )

    result = acquisition.acquire(request)

    assert [(item.start, item.end) for item in provider.requests] == [
        (date(2024, 1, 4), date(2024, 1, 10))
    ]
    assert result.manifest.cache.status is CacheStatus.STALE_REFRESH
    assert (
        result.frame.loc[result.frame["timestamp"] == pd.Timestamp("2024-01-04"), "open"].item()
        == 12.0
    )


def test_latest_session_is_not_completed_until_close_plus_lag(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 10), date(2024, 1, 10))
    before_available = datetime(2024, 1, 10, 21, 20, tzinfo=UTC)
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, store, _ = service(
        tmp_path, {Provider.YFINANCE: lambda: provider}, now=before_available
    )
    cached = canonical_frame(request, SESSIONS[-1:])
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        before_available - timedelta(days=2),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "actions",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=before_available,
            completed_at=before_available,
        ),
    )

    result = acquisition.acquire(request)

    assert provider.requests == []
    assert result.manifest.cache.status is CacheStatus.FULL_HIT


def test_stale_historical_segment_refreshes_its_full_relevant_history(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, store, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})
    cached = canonical_frame(request, SESSIONS)
    lineages = (
        LineageSegment(
            date(2024, 1, 2),
            date(2024, 1, 5),
            Provider.YFINANCE,
            NOW - timedelta(days=8),
            ActionCoverage.REPRESENTED,
            "a" * 64,
            "old",
        ),
        LineageSegment(
            date(2024, 1, 8),
            date(2024, 1, 10),
            Provider.YFINANCE,
            NOW - timedelta(minutes=20),
            ActionCoverage.REPRESENTED,
            "b" * 64,
            "recent",
        ),
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=lineages,
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    result = acquisition.acquire(request)

    assert [(item.start, item.end) for item in provider.requests] == [
        (date(2024, 1, 2), date(2024, 1, 5))
    ]
    assert result.manifest.cache.status is CacheStatus.STALE_REFRESH


def test_mixed_lineage_applies_historical_and_recent_ttls_independently(
    tmp_path: Path,
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    provider = FakeProvider(Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item))
    acquisition, store, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})
    cached = canonical_frame(request, SESSIONS)
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(days=8),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "old",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    acquisition.acquire(request)

    assert [(item.start, item.end) for item in provider.requests] == [
        (date(2024, 1, 2), date(2024, 1, 10))
    ]


def test_admitted_planning_failure_archives_failed_report(tmp_path: Path) -> None:
    calendar = FailingCloseCalendar()
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    acquisition, store, repository = service(tmp_path, {}, calendar=calendar)
    cached = canonical_frame(request, SESSIONS)
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(hours=2),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "old",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    with pytest.raises(ContractViolationError):
        acquisition.acquire(request)

    report = repository.lookup("acquisition-1")
    assert report is not None
    assert report["status"] == "failed"


def test_action_change_during_recent_overlap_reacquires_requested_history(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    provider = FakeProvider(
        Provider.YFINANCE, lambda item: native_batch(Provider.YFINANCE, item, dividend=0.5)
    )
    acquisition, store, _ = service(tmp_path, {Provider.YFINANCE: lambda: provider})
    cached = canonical_frame(request, SESSIONS)
    lineage = LineageSegment(
        request.start,
        request.end,
        Provider.YFINANCE,
        NOW - timedelta(hours=2),
        ActionCoverage.REPRESENTED,
        "a" * 64,
        "old-actions",
    )
    store.publish_generation(
        request,
        cached,
        {},
        AcquisitionManifest(
            "seed",
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(lineage,),
            started_at=NOW,
            completed_at=NOW,
        ),
    )

    result = acquisition.acquire(request)

    assert [(item.start, item.end) for item in provider.requests] == [
        (date(2024, 1, 4), date(2024, 1, 10)),
        (date(2024, 1, 2), date(2024, 1, 10)),
    ]
    assert result.frame["dividend_amount"].tolist() == [0.5] * 7
