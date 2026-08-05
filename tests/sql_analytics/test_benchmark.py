"""Deterministic SQLite benchmark and query-plan evidence tests."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from src.analytics.sql_benchmark import (
    BenchmarkConfig,
    BenchmarkRunner,
    _measure_timing,
)
from src.analytics.sql_contracts import QueryId


def _small_config(*, seed: int = 42) -> BenchmarkConfig:
    return BenchmarkConfig(
        seed=seed,
        run_count=4,
        equity_points_per_run=8,
        trades_per_run=3,
        warmups=1,
        repetitions=3,
    )


def _measurement_facts(report: object) -> tuple[tuple[str, str, int, bool, str], ...]:
    measurements = report.measurements  # type: ignore[attr-defined]
    return tuple(
        (
            measurement.schema_variant,
            measurement.query_id.value,
            measurement.result_row_count,
            measurement.contract_valid,
            measurement.result_sha256,
        )
        for measurement in measurements
    )


def test_same_seed_reproduces_fixture_identifiers_counts_and_result_hashes(
    tmp_path: Path,
) -> None:
    """Using global RNG state or independently varied fixture metadata breaks reproducibility."""
    config = _small_config()

    first = BenchmarkRunner(tmp_path / "first.db").run(config)
    second = BenchmarkRunner(tmp_path / "second.db").run(config)

    assert first.fixture is not None
    assert second.fixture is not None
    assert first.fixture == second.fixture
    assert first.fixture.table_row_counts == {
        "backtest_runs": 4,
        "equity_curve": 32,
        "metrics": 40,
        "trades": 12,
    }
    assert first.fixture.primary_identifiers == second.fixture.primary_identifiers
    assert _measurement_facts(first) == _measurement_facts(second)


def test_different_seed_changes_fixture_while_all_query_contracts_still_pass(
    tmp_path: Path,
) -> None:
    """Ignoring the configured seed would make distinct benchmark fixtures indistinguishable."""
    first = BenchmarkRunner(tmp_path / "seed-1.db").run(_small_config(seed=1))
    second = BenchmarkRunner(tmp_path / "seed-2.db").run(_small_config(seed=2))

    assert first.fixture is not None
    assert second.fixture is not None
    assert first.fixture.primary_identifiers != second.fixture.primary_identifiers
    assert first.fixture.value_sha256 != second.fixture.value_sha256
    assert all(measurement.contract_valid for measurement in first.measurements)
    assert all(measurement.contract_valid for measurement in second.measurements)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed", -1),
        ("seed", True),
        ("run_count", 0),
        ("run_count", 10_001),
        ("equity_points_per_run", 0),
        ("equity_points_per_run", 100_001),
        ("trades_per_run", 0),
        ("trades_per_run", 10_001),
        ("warmups", -1),
        ("warmups", 101),
        ("repetitions", 0),
        ("repetitions", 101),
    ],
)
def test_benchmark_config_rejects_invalid_or_resource_exhausting_values(
    field: str, value: int
) -> None:
    """Removing a bound permits invalid timing math or accidental unbounded fixture allocation."""
    values = {
        "seed": 42,
        "run_count": 4,
        "equity_points_per_run": 8,
        "trades_per_run": 3,
        "warmups": 1,
        "repetitions": 3,
    }
    values[field] = value

    with pytest.raises(ValueError):
        BenchmarkConfig(**values)


def test_baseline_and_hardened_variants_have_identical_results_and_distinct_indexes(
    tmp_path: Path,
) -> None:
    """Regenerating either copy or skipping migration evidence invalidates the comparison."""
    report = BenchmarkRunner(tmp_path / "hardened.db").run(_small_config())

    assert tuple(variant.name for variant in report.variants) == ("baseline", "hardened")
    baseline, hardened = report.variants
    assert baseline.alembic_revision == "455406e2c7ac"
    assert hardened.alembic_revision == "20260804_01"
    assert baseline.table_row_counts == hardened.table_row_counts
    assert "ix_trades_backtest_exit_id" not in baseline.indexes["trades"]
    assert "ix_backtest_runs_symbol_dates" not in baseline.indexes["backtest_runs"]
    assert "ix_trades_backtest_exit_id" in hardened.indexes["trades"]
    assert "ix_backtest_runs_symbol_dates" in hardened.indexes["backtest_runs"]

    expected_ids = {
        QueryId.STRATEGY_RUN_COMPARISON,
        QueryId.TRADE_SEQUENCE,
        QueryId.EQUITY_DRAWDOWN_AUDIT,
        QueryId.STRATEGY_COHORT_SUMMARY,
    }
    for query_id in expected_ids:
        matching = tuple(item for item in report.measurements if item.query_id is query_id)
        assert len(matching) == 2
        assert matching[0].result_row_count == matching[1].result_row_count
        assert matching[0].result_sha256 == matching[1].result_sha256
        assert all(item.contract_valid for item in matching)
        assert all(len(item.plan_rows) > 0 for item in matching)
        assert all(
            isinstance(row.node_id, int)
            and isinstance(row.parent_id, int)
            and isinstance(row.auxiliary, int)
            and isinstance(row.detail, str)
            for item in matching
            for row in item.plan_rows
        )


def test_injected_clock_excludes_warmups_and_uses_exact_nearest_rank_p95() -> None:
    """Timing warmups or interpolating p95 would misreport the raw measured samples."""
    calls = 0
    ticks = iter((100, 110, 200, 230, 400, 420, 700, 800))

    def execute() -> None:
        nonlocal calls
        calls += 1

    clock: Callable[[], int] = ticks.__next__
    summary = _measure_timing(execute, warmups=2, repetitions=4, clock=clock)

    assert calls == 6
    assert summary.samples_ns == (10, 30, 20, 100)
    assert summary.minimum_ns == 10
    assert summary.median_ns == 25.0
    assert summary.maximum_ns == 100
    assert summary.p95_ns == 100
