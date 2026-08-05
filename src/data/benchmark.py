"""Deterministic, offline verification benchmark for market-data acquisition.

The benchmark is deliberately a verification aid, not a claim about provider or
network performance.  Every sample uses an isolated cache and generated,
provider-shaped payloads, so a result can be reproduced without credentials.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import random
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.acquisition import AcquisitionService
from src.data.calendars import XNYSCalendar
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    ActionCoverage,
    CacheEvidence,
    CacheStatus,
    DataAcquisitionError,
    LineageSegment,
    Provider,
    ProviderBatch,
    ProviderCapabilities,
    TransientProviderError,
)
from src.data.manifest import ManifestRepository
from src.data.providers.base import ProviderEligibility
from src.data.retry import RetryExecutor
from src.data.store import DataStore

APPROVED_SCENARIOS = (
    "cold_cache",
    "full_hit",
    "partial_hit",
    "stale_refresh",
    "duplicate_removal",
    "limited_gaps",
    "retry_success",
    "yfinance_alpha_fallback",
    "corporate_action_invalidation",
    "fatal_rejection",
)

_NOW = datetime(2025, 1, 3, 22, tzinfo=UTC)
_DEPENDENCIES = (
    "filelock",
    "pandas",
    "pandas_market_calendars",
    "pyarrow",
    "requests",
)
_FIXED_CALENDAR = "XNYS"
_FIXED_INTERVAL = "1d"
_FIXED_SYMBOLS = ("SPY", "AAPL", "MSFT")
_FIXED_START = date(2020, 1, 1)
_FIXED_END = date(2024, 12, 31)
_FIXED_SEED = 42
_FIXED_WARMUPS = 3
_FIXED_MEASURED_RUNS = 15


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """The closed deterministic matrix used for comparable local verification."""

    calendar: str = _FIXED_CALENDAR
    interval: str = _FIXED_INTERVAL
    symbols: tuple[str, ...] = _FIXED_SYMBOLS
    start: date = _FIXED_START
    end: date = _FIXED_END
    seed: int = _FIXED_SEED
    warmups: int = _FIXED_WARMUPS
    measured_runs: int = _FIXED_MEASURED_RUNS

    def __post_init__(self) -> None:
        expected = {
            "calendar": _FIXED_CALENDAR,
            "interval": _FIXED_INTERVAL,
            "symbols": _FIXED_SYMBOLS,
            "start": _FIXED_START,
            "end": _FIXED_END,
            "seed": _FIXED_SEED,
            "warmups": _FIXED_WARMUPS,
            "measured_runs": _FIXED_MEASURED_RUNS,
        }
        for name in (
            "calendar",
            "interval",
            "symbols",
            "start",
            "end",
            "seed",
            "warmups",
            "measured_runs",
        ):
            if getattr(self, name) != expected[name]:
                raise ValueError(f"benchmark {name} must use the approved fixed value")


@dataclass(frozen=True, slots=True)
class _ProviderCall:
    sequence: int
    provider: Provider


@dataclass(slots=True)
class _ProviderCallLog:
    """Shared provider-fetch evidence, ordered at the actual fetch boundary."""

    calls: list[_ProviderCall]

    def __init__(self) -> None:
        self.calls = []

    def record(self, provider: Provider) -> None:
        self.calls.append(_ProviderCall(len(self.calls) + 1, provider))

    def trace(self) -> tuple[str, ...]:
        expected = list(range(1, len(self.calls) + 1))
        if [call.sequence for call in self.calls] != expected:
            raise AssertionError("benchmark provider call log is not monotonic")
        return tuple(call.provider.value for call in self.calls)


class _DeterministicProvider:
    def __init__(
        self,
        provider: Provider,
        fetch: Callable[[AcquisitionRequest, int], ProviderBatch],
        call_log: _ProviderCallLog,
    ) -> None:
        self._provider = provider
        self._fetch = fetch
        self._call_log = call_log
        self.requests: list[AcquisitionRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider=self._provider, supports_actions=True)

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility:
        del request
        return ProviderEligibility(True)

    def fetch(self, request: AcquisitionRequest) -> ProviderBatch:
        self._call_log.record(self._provider)
        self.requests.append(request)
        return self._fetch(request, len(self.requests))


@dataclass(slots=True)
class _ScenarioFixture:
    temporary: tempfile.TemporaryDirectory[str]
    scenario: str
    request: AcquisitionRequest
    sessions: pd.DatetimeIndex
    service: AcquisitionService
    store: DataStore
    repository: ManifestRepository
    providers: Mapping[Provider, _DeterministicProvider]
    provider_call_log: _ProviderCallLog
    expected_status: str
    expected_trace: tuple[str, ...]
    expected_counters: Mapping[str, int]

    def close(self) -> None:
        self.temporary.cleanup()


def deterministic_payload_hashes(config: BenchmarkConfig | None = None) -> dict[str, str]:
    """Return stable hashes for generated yfinance-shaped payloads, never market data."""
    applied = config or BenchmarkConfig()
    calendar = XNYSCalendar()
    sessions = calendar.expected_sessions(applied.start, applied.end)
    return {
        symbol: _payload_hash(
            _payload_rows(symbol, sessions, applied.seed),
        )
        for symbol in applied.symbols
    }


def percentile_95(values: tuple[float, ...]) -> float:
    """Use the documented nearest-rank p95 for a non-empty sample."""
    if not values:
        raise ValueError("p95 requires at least one timing")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def run_deterministic_benchmark(
    config: BenchmarkConfig | None = None,
    *,
    timer: Callable[[], float] = __import__("time").perf_counter,
) -> dict[str, Any]:
    """Run the approved offline matrix and return a JSON-safe artifact document.

    Fixture construction and teardown are intentionally outside every measured
    interval.  Live provider smoke checks belong in a separately supplied
    artifact and must never be compared with these timings.
    """
    applied = config or BenchmarkConfig()
    scenarios: list[dict[str, Any]] = []
    for scenario in APPROVED_SCENARIOS:
        for repetition in range(applied.warmups):
            fixture = _prepare_scenario_sample(applied, scenario, repetition)
            try:
                _run_scenario_sample(fixture)
            finally:
                fixture.close()

        samples: list[dict[str, Any]] = []
        for repetition in range(applied.measured_runs):
            fixture = _prepare_scenario_sample(applied, scenario, repetition)
            try:
                started = timer()
                result = _run_scenario_sample(fixture)
                elapsed = timer() - started
                samples.append({**result, "duration_seconds": max(0.0, elapsed)})
            finally:
                fixture.close()
        scenarios.append(_scenario_summary(scenario, applied.warmups, samples))

    calendar = XNYSCalendar()
    sessions = calendar.expected_sessions(applied.start, applied.end)
    return {
        "schema_version": "1",
        "kind": "deterministic_data_acquisition_benchmark",
        "configuration": _config_document(applied),
        "environment": _environment(),
        "deterministic": {
            "expected_sessions": len(sessions),
            "payload_hashes": deterministic_payload_hashes(applied),
            "scenarios": scenarios,
        },
        "live_smoke": None,
    }


def _config_document(config: BenchmarkConfig) -> dict[str, Any]:
    return {
        "calendar": config.calendar,
        "interval": config.interval,
        "symbols": list(config.symbols),
        "start": config.start.isoformat(),
        "end": config.end.isoformat(),
        "seed": config.seed,
        "warmups": config.warmups,
        "measured_runs": config.measured_runs,
        "percentile_method": "nearest_rank",
    }


def _environment() -> dict[str, Any]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "dependencies": {name: _dependency_version(name) for name in _DEPENDENCIES},
        "executable": sys.executable,
    }


def _dependency_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def _prepare_scenario_sample(
    config: BenchmarkConfig,
    scenario: str,
    repetition: int,
) -> _ScenarioFixture:
    if scenario not in APPROVED_SCENARIOS:
        raise ValueError(f"unsupported benchmark scenario: {scenario}")
    temporary = tempfile.TemporaryDirectory(prefix="data-acquisition-benchmark-")
    root = Path(temporary.name)
    symbol = config.symbols[APPROVED_SCENARIOS.index(scenario) % len(config.symbols)]
    request = AcquisitionRequest(symbol, config.start, config.end)
    calendar = XNYSCalendar()
    sessions = calendar.expected_sessions(request.start, request.end)
    identifiers = iter((f"seed-{scenario}-{repetition}", f"sample-{scenario}-{repetition}"))
    repository = ManifestRepository(
        root / "reports",
        id_factory=lambda: next(identifiers),
        clock=lambda: _NOW,
    )
    generation = 0

    def generation_id() -> str:
        nonlocal generation
        generation += 1
        return f"generation-{generation}"

    store = DataStore(
        root / "cache",
        calendar_versions=calendar.version_evidence(),
        generation_id_factory=generation_id,
        clock=lambda: _NOW,
        manifest_repository=repository,
    )
    frame = _canonical_frame(request, sessions, config.seed)
    provider_call_log = _ProviderCallLog()
    providers = _scenario_providers(scenario, frame, sessions, config.seed, provider_call_log)
    service = AcquisitionService(
        store=store,
        manifest_repository=repository,
        calendar=calendar,
        provider_factories={
            provider: _provider_factory(instance) for provider, instance in providers.items()
        },
        retry_executor=RetryExecutor(
            clock=lambda: _NOW,
            sleeper=lambda _: None,
            random_source=lambda: 0.0,
        ),
        clock=lambda: _NOW,
    )
    fixture = _ScenarioFixture(
        temporary,
        scenario,
        request,
        sessions,
        service,
        store,
        repository,
        providers,
        provider_call_log,
        _expected_status(scenario),
        _expected_trace(scenario),
        _expected_counters(scenario, len(sessions)),
    )
    _seed_scenario(fixture, frame)
    _assert_preconditions(fixture)
    return fixture


def _scenario_providers(
    scenario: str,
    frame: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    seed: int,
    call_log: _ProviderCallLog,
) -> dict[Provider, _DeterministicProvider]:
    def batch(
        provider: Provider,
        request: AcquisitionRequest,
        native: pd.DataFrame,
    ) -> ProviderBatch:
        return ProviderBatch(
            provider,
            request,
            native,
            received_at=_NOW,
            raw_row_count=len(native),
            action_coverage=ActionCoverage.REPRESENTED,
        )

    def normal(provider: Provider, request: AcquisitionRequest, _: int) -> ProviderBatch:
        selected = _requested_canonical(frame, request)
        return batch(provider, request, _native_frame(provider, selected))

    yfinance = _DeterministicProvider(
        Provider.YFINANCE,
        lambda request, call: normal(Provider.YFINANCE, request, call),
        call_log,
    )
    if scenario == "duplicate_removal":
        def duplicate(request: AcquisitionRequest, _: int) -> ProviderBatch:
            selected = _requested_canonical(frame, request)
            native = _native_frame(Provider.YFINANCE, selected)
            return batch(Provider.YFINANCE, request, pd.concat([native, native.iloc[:1]]))

        yfinance = _DeterministicProvider(Provider.YFINANCE, duplicate, call_log)
    elif scenario == "limited_gaps":
        def gap(request: AcquisitionRequest, _: int) -> ProviderBatch:
            selected = _requested_canonical(frame, request).iloc[1:].copy(deep=True)
            return batch(Provider.YFINANCE, request, _native_frame(Provider.YFINANCE, selected))

        yfinance = _DeterministicProvider(Provider.YFINANCE, gap, call_log)
    elif scenario == "retry_success":
        def retry(request: AcquisitionRequest, call: int) -> ProviderBatch:
            if call == 1:
                raise TransientProviderError("generated transient benchmark failure")
            return normal(Provider.YFINANCE, request, call)

        yfinance = _DeterministicProvider(Provider.YFINANCE, retry, call_log)
    elif scenario == "yfinance_alpha_fallback":
        def invalid(request: AcquisitionRequest, _: int) -> ProviderBatch:
            selected = _requested_canonical(frame, request)
            native = _native_frame(Provider.YFINANCE, selected)
            native["High"] = native["Open"] - 1.0
            return batch(Provider.YFINANCE, request, native)

        yfinance = _DeterministicProvider(Provider.YFINANCE, invalid, call_log)
    elif scenario == "corporate_action_invalidation":
        def changed_actions(request: AcquisitionRequest, _: int) -> ProviderBatch:
            selected = _requested_canonical(frame, request)
            native = _native_frame(Provider.YFINANCE, selected, dividend=1.0)
            return batch(Provider.YFINANCE, request, native)

        yfinance = _DeterministicProvider(Provider.YFINANCE, changed_actions, call_log)
    elif scenario == "fatal_rejection":
        def reject(request: AcquisitionRequest, _: int) -> ProviderBatch:
            selected = _requested_canonical(frame, request)
            native = _native_frame(Provider.YFINANCE, selected)
            native["High"] = native["Open"] - 1.0
            return batch(Provider.YFINANCE, request, native)

        yfinance = _DeterministicProvider(Provider.YFINANCE, reject, call_log)

    providers = {Provider.YFINANCE: yfinance}
    if scenario == "yfinance_alpha_fallback":
        providers[Provider.ALPHA_VANTAGE] = _DeterministicProvider(
            Provider.ALPHA_VANTAGE,
            lambda request, call: normal(Provider.ALPHA_VANTAGE, request, call),
            call_log,
        )
    del sessions, seed
    return providers


def _provider_factory(
    provider: _DeterministicProvider,
) -> Callable[[], _DeterministicProvider]:
    def create() -> _DeterministicProvider:
        return provider

    return create


def _seed_scenario(fixture: _ScenarioFixture, frame: pd.DataFrame) -> None:
    if fixture.scenario not in {
        "full_hit",
        "partial_hit",
        "stale_refresh",
        "corporate_action_invalidation",
    }:
        return
    cached = frame
    seed_request = fixture.request
    acquired_at = _NOW
    if fixture.scenario == "partial_hit":
        cached = frame.iloc[:-5].copy(deep=True)
        seed_request = AcquisitionRequest(
            fixture.request.symbol,
            cached.iloc[0]["timestamp"].date(),
            cached.iloc[-1]["timestamp"].date(),
        )
    elif fixture.scenario == "stale_refresh":
        acquired_at = _NOW - timedelta(days=8)
    elif fixture.scenario == "corporate_action_invalidation":
        acquired_at = _NOW - timedelta(hours=2)
    lineage = LineageSegment(
        cached.iloc[0]["timestamp"].date(),
        cached.iloc[-1]["timestamp"].date(),
        Provider.YFINANCE,
        acquired_at,
        ActionCoverage.REPRESENTED,
        _frame_hash(cached),
        "0" * 64,
    )
    fixture.store.publish_generation(
        seed_request,
        cached,
        {"benchmark_seed": fixture.scenario},
        AcquisitionManifest(
            f"seed-{fixture.scenario}",
            seed_request,
            AcquisitionStatus.SUCCESS,
            cache=CacheEvidence(CacheStatus.MISS),
            lineage=(lineage,),
            started_at=acquired_at,
            completed_at=acquired_at,
        ),
    )


def _assert_preconditions(fixture: _ScenarioFixture) -> None:
    cached = fixture.store.read_generation(fixture.request)
    if fixture.scenario in {"full_hit", "stale_refresh", "corporate_action_invalidation"}:
        assert cached.frame is not None and len(cached.frame) == len(fixture.sessions)
    elif fixture.scenario == "partial_hit":
        assert cached.frame is not None and len(cached.frame) == len(fixture.sessions) - 5
    else:
        assert cached.frame is None


def _run_scenario_sample(fixture: _ScenarioFixture) -> dict[str, Any]:
    result = None
    report: Mapping[str, Any] | None = None
    try:
        result = fixture.service.acquire(fixture.request)
        report = result.manifest.to_dict()
    except DataAcquisitionError as error:
        if error.acquisition_id is not None:
            report = fixture.repository.lookup(error.acquisition_id)
    if report is None:
        raise AssertionError("benchmark scenario did not preserve an acquisition report")
    cache = report.get("cache")
    if not isinstance(cache, Mapping) or cache.get("status") != fixture.expected_status:
        raise AssertionError("benchmark scenario produced an unexpected cache status")
    trace = fixture.provider_call_log.trace()
    if trace != fixture.expected_trace:
        raise AssertionError(f"benchmark provider trace mismatch: {trace!r}")
    counters = _validated_counters(report.get("counters"), fixture.expected_counters)
    scenario_counters = _scenario_counters(fixture.scenario, report)
    for name, expected in _expected_scenario_counters(fixture.scenario).items():
        if scenario_counters.get(name) != expected:
            raise AssertionError(f"benchmark scenario counter {name} did not reconcile")
    return {
        "cache_status": fixture.expected_status,
        "provider_calls": dict(sorted(Counter(trace).items())),
        "provider_trace": list(trace),
        "counts": {
            "expected_sessions": len(fixture.sessions),
            "returned_rows": 0 if result is None else len(result.frame),
        },
        "counters": dict(counters),
        "scenario_counters": scenario_counters,
        "findings": _finding_codes(report),
        "parquet_bytes": _parquet_bytes(fixture.store, fixture.request),
        "output_hash": report.get("output_hash"),
        "outcome": "success" if result is not None else "failed",
    }


def _validated_counters(
    observed: object,
    expected: Mapping[str, int],
) -> dict[str, Any]:
    """Require and reconcile the manifest counters used by one benchmark scenario."""
    if not isinstance(observed, Mapping):
        raise AssertionError("benchmark report counters are missing or invalid")
    counters = {str(name): value for name, value in observed.items()}
    required = set(expected)
    if required:
        required.update(
            {"expected_sessions", "accepted_expected_sessions", "missing_sessions"}
        )
    missing = sorted(name for name in required if name not in counters)
    if missing:
        raise AssertionError(f"benchmark report counters are missing: {', '.join(missing)}")
    for name in required:
        value = counters[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise AssertionError(f"benchmark counter {name} is not an integer")
    for name, value in expected.items():
        if counters[name] != value:
            raise AssertionError(f"benchmark counter {name} did not reconcile")
    if required and counters["expected_sessions"] != (
        counters["accepted_expected_sessions"] + counters["missing_sessions"]
    ):
        raise AssertionError("benchmark counters did not reconcile")
    return counters


def _scenario_summary(scenario: str, warmups: int, samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) != _FIXED_MEASURED_RUNS:
        raise AssertionError("benchmark did not collect the approved number of measured runs")
    timings = tuple(float(item["duration_seconds"]) for item in samples)
    first = samples[0]
    for item in samples[1:]:
        for name in (
            "cache_status",
            "provider_calls",
            "provider_trace",
            "counts",
            "counters",
            "scenario_counters",
        ):
            if item[name] != first[name]:
                raise AssertionError(
                    f"benchmark scenario {scenario} was not deterministic for {name}"
                )
    return {
        "name": scenario,
        "warmup_runs": warmups,
        "samples": samples,
        "median_seconds": float(pd.Series(timings).median()),
        "p95_seconds": percentile_95(timings),
        "cache_status": first["cache_status"],
        "provider_calls": first["provider_calls"],
        "counts": first["counts"],
        "counters": first["counters"],
        "scenario_counters": first["scenario_counters"],
        "findings": first["findings"],
        "parquet_bytes": first["parquet_bytes"],
        "output_hash": first["output_hash"],
    }


def _expected_status(scenario: str) -> str:
    return {
        "cold_cache": "miss",
        "full_hit": "full_hit",
        "partial_hit": "partial_hit",
        "stale_refresh": "stale_refresh",
        "duplicate_removal": "miss",
        "limited_gaps": "miss",
        "retry_success": "miss",
        "yfinance_alpha_fallback": "miss",
        "corporate_action_invalidation": "stale_refresh",
        "fatal_rejection": "miss",
    }[scenario]


def _expected_trace(scenario: str) -> tuple[str, ...]:
    return {
        "cold_cache": ("yfinance",),
        "full_hit": (),
        "partial_hit": ("yfinance",),
        "stale_refresh": ("yfinance",),
        "duplicate_removal": ("yfinance",),
        "limited_gaps": ("yfinance",),
        "retry_success": ("yfinance", "yfinance"),
        "yfinance_alpha_fallback": ("yfinance", "alpha_vantage"),
        "corporate_action_invalidation": ("yfinance", "yfinance"),
        "fatal_rejection": ("yfinance",),
    }[scenario]


def _expected_counters(scenario: str, sessions: int) -> Mapping[str, int]:
    if scenario == "duplicate_removal":
        return {"expected_sessions": sessions}
    if scenario == "limited_gaps":
        return {"expected_sessions": sessions, "missing_sessions": 1}
    if scenario == "fatal_rejection":
        return {}
    return {
        "expected_sessions": sessions,
        "accepted_expected_sessions": sessions,
        "missing_sessions": 0,
    }


def _expected_scenario_counters(scenario: str) -> Mapping[str, int]:
    return {"exact_duplicate_rows_removed": 1} if scenario == "duplicate_removal" else {}


def _scenario_counters(scenario: str, report: Mapping[str, Any]) -> dict[str, int]:
    if scenario != "duplicate_removal":
        return {}
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return {}
    for item in findings:
        if not isinstance(item, Mapping) or item.get("code") != "exact_duplicates_removed":
            continue
        details = item.get("details")
        if isinstance(details, Mapping) and isinstance(details.get("rows"), int):
            return {"exact_duplicate_rows_removed": details["rows"]}
    return {}


def _canonical_frame(
    request: AcquisitionRequest,
    sessions: pd.DatetimeIndex,
    seed: int,
) -> pd.DataFrame:
    rows = _payload_rows(request.symbol, sessions, seed)
    frame = pd.DataFrame.from_records(rows)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).astype("datetime64[ns]")
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


def _payload_rows(symbol: str, sessions: pd.DatetimeIndex, seed: int) -> list[dict[str, Any]]:
    symbol_index = ("SPY", "AAPL", "MSFT").index(symbol)
    generator = random.Random(seed + symbol_index)
    rows: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        base = 100.0 + (symbol_index * 20.0) + index * 0.02 + generator.random()
        rows.append(
            {
                "timestamp": pd.Timestamp(session).date().isoformat(),
                "symbol": symbol,
                "open": round(base, 6),
                "high": round(base + 1.0, 6),
                "low": round(base - 1.0, 6),
                "close": round(base + 0.25, 6),
                "volume": float(1_000_000 + index),
                "adj_close": round(base + 0.25, 6),
                "dividend_amount": 0.0,
                "split_coefficient": 1.0,
                "source": Provider.YFINANCE.value,
            }
        )
    return rows


def _native_frame(
    provider: Provider,
    frame: pd.DataFrame,
    *,
    dividend: float = 0.0,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(frame["timestamp"])
    if provider is Provider.YFINANCE:
        return pd.DataFrame(
            {
                "Open": frame["open"].tolist(),
                "High": frame["high"].tolist(),
                "Low": frame["low"].tolist(),
                "Close": frame["close"].tolist(),
                "Volume": frame["volume"].tolist(),
                "Adj Close": frame["adj_close"].tolist(),
                "Dividends": [dividend] * len(frame),
                "Stock Splits": frame["split_coefficient"].tolist(),
            },
            index=index,
        )
    return pd.DataFrame(
        {
            "1. open": frame["open"].tolist(),
            "2. high": frame["high"].tolist(),
            "3. low": frame["low"].tolist(),
            "4. close": frame["close"].tolist(),
            "5. adjusted close": frame["adj_close"].tolist(),
            "6. volume": frame["volume"].tolist(),
            "7. dividend amount": [dividend] * len(frame),
            "8. split coefficient": frame["split_coefficient"].tolist(),
        },
        index=index,
    )


def _requested_canonical(frame: pd.DataFrame, request: AcquisitionRequest) -> pd.DataFrame:
    return frame[
        (frame["timestamp"] >= pd.Timestamp(request.start))
        & (frame["timestamp"] <= pd.Timestamp(request.end))
    ].copy(deep=True)


def _payload_hash(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _frame_hash(frame: pd.DataFrame) -> str:
    rows = frame.assign(timestamp=frame["timestamp"].map(lambda item: item.date().isoformat()))
    records = [
        {str(key): value for key, value in record.items()}
        for record in rows.to_dict(orient="records")
    ]
    return _payload_hash(records)


def _finding_codes(report: Mapping[str, Any]) -> list[str]:
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return []
    return [str(item["code"]) for item in findings if isinstance(item, Mapping) and "code" in item]


def _parquet_bytes(store: DataStore, request: AcquisitionRequest) -> int:
    generation = store.current_generation_id(request)
    if generation is None:
        return 0
    path = store.generation_namespace(request) / "generations" / generation / "bars.parquet"
    return path.stat().st_size if path.is_file() else 0
