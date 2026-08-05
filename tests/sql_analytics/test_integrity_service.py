"""Integration coverage for the read-only SQL integrity report."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from alembic import command
from src.analytics.sql_contracts import Severity, ValidationFinding, ValidationReport
from src.analytics.sql_service import IntegrityFailureError, IntegrityService
from src.db.database import create_db_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "455406e2c7ac"


def _find(report: ValidationReport, code: str) -> ValidationFinding:
    matches = tuple(finding for finding in report.findings if finding.code == code)
    assert len(matches) == 1, code
    return matches[0]


@pytest.fixture()
def empty_integrity_db(tmp_path: Path) -> Iterator[Engine]:
    database_url = f"sqlite:///{tmp_path / 'integrity.db'}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, BASELINE_REVISION)
    engine = create_db_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_clean_database_reports_exact_counts_and_versioned_immutable_contract(
    analytics_db: Engine,
) -> None:
    report = IntegrityService(analytics_db).validate(tolerance=0.02)

    assert report.schema_version == "1.0"
    assert report.generated_at.tzinfo is not None
    assert report.database.endswith("analytics.db")
    assert report.tolerance == 0.02
    assert report.has_failures is False
    assert _find(report, "TABLE_COUNT_BACKTEST_RUNS") == ValidationFinding(
        code="TABLE_COUNT_BACKTEST_RUNS",
        severity=Severity.PASS,
        table="backtest_runs",
        observed_count=3,
        sample_ids=(),
        message="Counted 3 rows in backtest_runs.",
    )
    assert _find(report, "TABLE_COUNT_TRADES").observed_count == 4
    assert _find(report, "TABLE_COUNT_EQUITY_CURVE").observed_count == 7
    assert _find(report, "TABLE_COUNT_METRICS").observed_count == 30
    assert _find(report, "PER_RUN_CHILD_COUNTS") == ValidationFinding(
        code="PER_RUN_CHILD_COUNTS",
        severity=Severity.PASS,
        table=None,
        observed_count=3,
        sample_ids=(
            "run-ma:trades=3,equity=3,metrics=10",
            "run-other:trades=0,equity=2,metrics=10",
            "run-rsi:trades=1,equity=2,metrics=10",
        ),
        message="Reconciled child-row counts for 3 backtest runs.",
    )
    assert _find(report, "DUPLICATE_METRIC_KEYS").severity is Severity.PASS
    assert _find(report, "METRIC_ROW_COUNT_RECONCILIATION").severity is Severity.PASS
    assert _find(report, "DUPLICATE_EQUITY_TIMESTAMPS").severity is Severity.PASS
    assert _find(report, "ORPHAN_TRADES").severity is Severity.PASS
    assert _find(report, "ORPHAN_EQUITY_POINTS").severity is Severity.PASS
    assert _find(report, "ORPHAN_METRICS").severity is Severity.PASS
    with pytest.raises(FrozenInstanceError):
        report.findings += ()  # type: ignore[misc]


@pytest.mark.parametrize("sample_limit", [0, 101, -1, True, 1.5])
def test_validate_rejects_invalid_sample_limits(
    empty_integrity_db: Engine, sample_limit: object
) -> None:
    with pytest.raises(ValueError, match="sample_limit must be an integer from 1 to 100"):
        IntegrityService(empty_integrity_db).validate(
            tolerance=0.0,
            sample_limit=sample_limit,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("tolerance", [-0.1, float("nan"), float("inf"), True])
def test_integrity_methods_reject_invalid_tolerance(
    empty_integrity_db: Engine, tolerance: float
) -> None:
    service = IntegrityService(empty_integrity_db)
    with pytest.raises(ValueError, match="tolerance must be a finite non-negative number"):
        service.validate(tolerance)
    with pytest.raises(ValueError, match="tolerance must be a finite non-negative number"):
        service.failures_for_run_ids(("run",), tolerance)


@pytest.mark.parametrize("run_ids", ["bad-24", b"bad-24", ("",), ("valid", 1)])
def test_selected_run_ids_reject_scalar_blank_and_non_string_values(
    empty_integrity_db: Engine, run_ids: object
) -> None:
    with pytest.raises(ValueError, match="run_ids must contain non-empty strings"):
        IntegrityService(empty_integrity_db).failures_for_run_ids(
            run_ids,  # type: ignore[arg-type]
            0.0,
        )


def test_validation_uses_one_connection_snapshot(empty_integrity_db: Engine) -> None:
    checkout_count = 0

    def count_checkout(_connection: object, _record: object, _proxy: object) -> None:
        nonlocal checkout_count
        checkout_count += 1

    event.listen(empty_integrity_db, "checkout", count_checkout)
    try:
        IntegrityService(empty_integrity_db).validate(0.0)
    finally:
        event.remove(empty_integrity_db, "checkout", count_checkout)

    assert checkout_count == 1


def _insert_controlled_defects(connection: Connection) -> None:
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.exec_driver_sql(
        """
        INSERT INTO backtest_runs
            (id, strategy_name, symbol, start_date, end_date, params_json,
             initial_capital, commission_pct, slippage_pct, created_at)
        VALUES
            ('bad-run', 'bad', 'SPY', '2024-02-02', '2024-02-01', '{}',
             10000, 0.001, 0.0005, '2024-02-03'),
            ('bad-fees-run', 'bad', 'SPY', '2024-01-01', '2024-01-02', '{}',
             0, -0.001, -0.0005, '2024-02-03'),
            ('clean-run', 'clean', 'SPY', '2024-01-01', '2024-01-02', '{}',
             10000, 0.001, 0.0005, '2024-02-03')
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO metrics (id, backtest_id, metric_name, metric_value) VALUES
            (101, 'bad-run', 'total_return', 0.50),
            (102, 'bad-run', 'total_return', 0.25),
            (103, 'bad-run', 'max_drawdown', -0.50),
            (104, 'ghost-run', 'total_return', 0.01)
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO equity_curve (id, backtest_id, date, equity, drawdown_pct) VALUES
            (201, 'bad-run', '2024-01-01', 10000, 0.0),
            (202, 'bad-run', '2024-01-02', 11000, 0.0),
            (203, 'bad-run', '2024-01-02', 9000, -0.01),
            (204, 'ghost-run', '2024-01-01', 10000, 0.0),
            (205, 'clean-run', '2024-01-01', 10000, 0.0),
            (206, 'bad-fees-run', '2024-01-01', 10000, 0.0)
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO trades
            (id, backtest_id, entry_date, exit_date, direction, entry_price, exit_price,
             quantity, pnl, pnl_pct, commission)
        VALUES
            (301, 'bad-run', '2024-01-01', '2024-01-02', 'LONG', 100, NULL,
             0, NULL, NULL, 0),
            (302, 'ghost-run', '2024-01-01', NULL, 'LONG', 100, NULL,
             1, NULL, NULL, 0)
        """
    )
    connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _database_snapshot(engine: Engine) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    tables = ("backtest_runs", "trades", "equity_curve", "metrics")
    with engine.connect() as connection:
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id")
                ),
            )
            for table in tables
        )


def test_controlled_legacy_defects_have_exact_codes_counts_and_capped_samples(
    empty_integrity_db: Engine,
) -> None:
    with empty_integrity_db.connect() as connection:
        _insert_controlled_defects(connection)
    before = _database_snapshot(empty_integrity_db)

    report = IntegrityService(empty_integrity_db).validate(tolerance=0.001, sample_limit=1)

    expected = {
        "DUPLICATE_METRIC_KEYS": ("metrics", 1, ("bad-run:total_return",)),
        "METRIC_ROW_COUNT_RECONCILIATION": ("metrics", 1, ("bad-run",)),
        "DUPLICATE_EQUITY_TIMESTAMPS": ("equity_curve", 1, ("bad-run:2024-01-02",)),
        "ORPHAN_TRADES": ("trades", 1, ("302",)),
        "ORPHAN_EQUITY_POINTS": ("equity_curve", 1, ("204",)),
        "ORPHAN_METRICS": ("metrics", 1, ("104",)),
        "INVALID_RUN_DATE_RANGE": ("backtest_runs", 1, ("bad-run",)),
        "NONPOSITIVE_INITIAL_CAPITAL": ("backtest_runs", 1, ("bad-fees-run",)),
        "NEGATIVE_COMMISSION_PCT": ("backtest_runs", 1, ("bad-fees-run",)),
        "NEGATIVE_SLIPPAGE_PCT": ("backtest_runs", 1, ("bad-fees-run",)),
        "NONPOSITIVE_TRADE_QUANTITY": ("trades", 1, ("301",)),
        "INCONSISTENT_TRADE_CLOSE_FIELDS": ("trades", 1, ("301",)),
        "TOTAL_RETURN_MISMATCH": ("metrics", 1, ("bad-run",)),
        "DRAWDOWN_MISMATCH": ("equity_curve", 1, ("203",)),
    }
    for code, (table, count, samples) in expected.items():
        finding = _find(report, code)
        assert (finding.severity, finding.table, finding.observed_count, finding.sample_ids) == (
            Severity.FAIL,
            table,
            count,
            samples,
        )
    assert _find(report, "SQLITE_FOREIGN_KEY_CHECK").observed_count == 3
    assert _find(report, "NO_CLOSED_TRADES").severity is Severity.WARN
    assert _find(report, "MISSING_OPTIONAL_METRICS").severity is Severity.WARN
    assert _find(report, "SPARSE_EQUITY_HISTORY").severity is Severity.WARN

    assert _database_snapshot(empty_integrity_db) == before


def _insert_many_invalid_runs(connection: Connection) -> None:
    rows = []
    trades = []
    for index in range(25):
        run_id = f"bad-{index:02d}"
        rows.append(
            (
                run_id,
                "strategy",
                "SPY",
                "2024-01-01",
                "2024-01-02",
                "{}",
                10000.0,
                0.0,
                0.0,
                "2024-01-03",
            )
        )
        trades.append(
            (
                index + 1,
                run_id,
                "2024-01-01",
                None,
                "LONG",
                100.0,
                None,
                0,
                None,
                None,
                0.0,
            )
        )
    rows.append(
        (
            "clean-warning",
            "strategy",
            "SPY",
            "2024-01-01",
            "2024-01-02",
            "{}",
            10000.0,
            0.0,
            0.0,
            "2024-01-03",
        )
    )
    connection.exec_driver_sql(
        "INSERT INTO backtest_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.exec_driver_sql(
        "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", trades
    )
    connection.exec_driver_sql(
        """
        INSERT INTO equity_curve (backtest_id, date, equity, drawdown_pct)
        VALUES ('clean-warning', '2024-01-01', 10000.0, 0.0)
        """
    )
    connection.commit()


def test_selected_run_failures_are_scoped_beyond_presentation_cap(
    empty_integrity_db: Engine,
) -> None:
    with empty_integrity_db.connect() as connection:
        _insert_many_invalid_runs(connection)
    service = IntegrityService(empty_integrity_db)

    global_finding = _find(service.validate(0.0), "NONPOSITIVE_TRADE_QUANTITY")
    assert global_finding.observed_count == 25
    assert len(global_finding.sample_ids) == 20
    assert "25" not in global_finding.sample_ids

    scoped = service.failures_for_run_ids(("bad-24",), tolerance=0.0)
    assert tuple(finding.code for finding in scoped) == (
        "MISSING_REQUIRED_EQUITY_HISTORY",
        "NONPOSITIVE_TRADE_QUANTITY",
    )
    assert _find(
        ValidationReport("1.0", datetime.now().astimezone(), "test", 0.0, scoped),
        "NONPOSITIVE_TRADE_QUANTITY",
    ).sample_ids == ("25",)
    assert service.failures_for_run_ids(("clean-warning",), tolerance=0.0) == ()


def test_empty_selected_run_ids_execute_no_sql(empty_integrity_db: Engine) -> None:
    statements: list[str] = []

    def record_sql(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(empty_integrity_db, "before_cursor_execute", record_sql)
    try:
        assert IntegrityService(empty_integrity_db).failures_for_run_ids((), 0.0) == ()
    finally:
        event.remove(empty_integrity_db, "before_cursor_execute", record_sql)
    assert statements == []


def test_integrity_failure_error_retains_immutable_report(empty_integrity_db: Engine) -> None:
    report = IntegrityService(empty_integrity_db).validate(0.0)
    error = IntegrityFailureError(report)

    assert error.report is report
    assert str(error) == "Integrity validation found fatal failures"
