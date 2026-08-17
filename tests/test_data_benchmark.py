"""Deterministic verification benchmark contract tests."""

from __future__ import annotations

from datetime import date

import pytest

from src.data.benchmark import (
    APPROVED_SCENARIOS,
    BenchmarkConfig,
    _DeterministicProvider,
    _prepare_scenario_sample,
    _ProviderCallLog,
    _run_scenario_sample,
    _validated_counters,
    deterministic_payload_hashes,
    percentile_95,
    run_deterministic_benchmark,
)
from src.data.contracts import AcquisitionRequest, Provider


def test_benchmark_configuration_is_the_approved_fixed_matrix() -> None:
    config = BenchmarkConfig()

    assert config.calendar == "XNYS"
    assert config.interval == "1d"
    assert config.symbols == ("SPY", "AAPL", "MSFT")
    assert config.start.isoformat() == "2020-01-01"
    assert config.end.isoformat() == "2024-12-31"
    assert config.seed == 42
    assert config.warmups == 3
    assert config.measured_runs == 15

    with pytest.raises(ValueError, match="seed"):
        BenchmarkConfig(seed=7)


def test_payload_hashes_are_stable_and_cover_the_fixed_symbols() -> None:
    config = BenchmarkConfig()

    first = deterministic_payload_hashes(config)
    second = deterministic_payload_hashes(config)

    assert first == second
    assert set(first) == {"SPY", "AAPL", "MSFT"}
    assert all(len(value) == 64 for value in first.values())


def test_scenario_order_and_percentile_are_stable() -> None:
    assert APPROVED_SCENARIOS == (
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
    assert percentile_95(tuple(float(value) for value in range(1, 16))) == 15.0


@pytest.mark.parametrize("counters", (None, {}, {"expected_sessions": 3}))
def test_required_reconciliation_counters_fail_closed(counters: object) -> None:
    with pytest.raises(AssertionError, match="counters"):
        _validated_counters(
            counters,
            {
                "expected_sessions": 3,
                "accepted_expected_sessions": 3,
                "missing_sessions": 0,
            },
        )


def test_provider_trace_is_the_shared_fetch_entry_order() -> None:
    calls = _ProviderCallLog()
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 2))
    yfinance = _DeterministicProvider(Provider.YFINANCE, lambda *_: None, calls)
    alpha = _DeterministicProvider(Provider.ALPHA_VANTAGE, lambda *_: None, calls)

    alpha.fetch(request)
    yfinance.fetch(request)

    assert calls.trace() == ("alpha_vantage", "yfinance")


@pytest.mark.parametrize("scenario", APPROVED_SCENARIOS)
def test_each_scenario_has_a_fresh_reconciled_real_service_fixture(scenario: str) -> None:
    fixture = _prepare_scenario_sample(BenchmarkConfig(), scenario, 0)
    try:
        sample = _run_scenario_sample(fixture)
    finally:
        fixture.close()

    assert sample["cache_status"] == fixture.expected_status
    assert tuple(sample["provider_trace"]) == fixture.expected_trace
    assert sample["counts"]["expected_sessions"] > 0
    counters = sample["counters"]
    if counters:
        assert counters["expected_sessions"] == (
            counters["accepted_expected_sessions"] + counters["missing_sessions"]
        )
    if scenario == "duplicate_removal":
        assert sample["scenario_counters"] == {"exact_duplicate_rows_removed": 1}


def test_benchmark_reports_fixed_sample_count_environment_and_separate_live_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter(
        {
            "cache_status": "miss",
            "provider_calls": {"yfinance": 1},
            "provider_trace": ["yfinance"],
            "counts": {"expected_sessions": 1, "returned_rows": 1},
            "counters": {
                "expected_sessions": 1,
                "accepted_expected_sessions": 1,
                "missing_sessions": 0,
            },
            "scenario_counters": {},
            "findings": (),
            "parquet_bytes": 10,
            "output_hash": "a" * 64,
            "outcome": "success",
        }
        for _ in range(len(APPROVED_SCENARIOS) * (3 + 15))
    )

    def next_outcome(*_: object) -> dict[str, object]:
        return next(outcomes)

    monkeypatch.setattr("src.data.benchmark._run_scenario_sample", next_outcome)
    timer_values = iter(value / 10 for value in range(1000))

    def timer() -> float:
        return next(timer_values)

    artifact = run_deterministic_benchmark(timer=timer)

    scenarios = artifact["deterministic"]["scenarios"]
    assert [item["name"] for item in scenarios] == list(APPROVED_SCENARIOS)
    assert all(item["warmup_runs"] == 3 for item in scenarios)
    assert all(len(item["samples"]) == 15 for item in scenarios)
    assert all(item["median_seconds"] == pytest.approx(0.1) for item in scenarios)
    assert all(item["p95_seconds"] == pytest.approx(0.1) for item in scenarios)
    assert artifact["environment"]["python_version"]
    assert artifact["live_smoke"] is None
