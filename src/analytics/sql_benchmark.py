"""Deterministic SQLite benchmark and query-plan evidence."""
from __future__ import annotations

import json
import math
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias, cast
from urllib.parse import quote

import pandas as pd
import sqlalchemy
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.schema import Table

from alembic import command
from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import (
    COHORT_SUMMARY_CONTRACT,
    COMPARISON_CONTRACT,
    EQUITY_DRAWDOWN_AUDIT_CONTRACT,
    TRADE_SEQUENCE_CONTRACT,
    BindValue,
    QueryId,
    ResultContract,
    Scalar,
)
from src.analytics.sql_service import AnalyticsRepository, validate_frame
from src.db.database import create_db_engine
from src.db.tables import BacktestRun, EquityCurvePoint, MetricRecord, TradeRecord

_BASELINE_REVISION = "455406e2c7ac"
_HEAD_REVISION = "20260804_01"
_SCHEMA_VERSION = "1.0"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APPLICATION_TABLES = ("backtest_runs", "equity_curve", "metrics", "trades")
_BATCH_SIZE = 1_000
_MAX_RUNS = 10_000
_MAX_EQUITY_POINTS_PER_RUN = 100_000
_MAX_TRADES_PER_RUN = 10_000
_MAX_TOTAL_ROWS = 5_000_000
_MAX_WARMUPS = 100
_MAX_REPETITIONS = 100
_METRIC_NAMES = (
    "sharpe_ratio",
    "sortino_ratio",
    "cagr",
    "max_drawdown",
    "max_drawdown_duration",
    "win_rate",
    "profit_factor",
    "calmar_ratio",
    "total_trades",
    "total_return",
)
_STRATEGY_NAMES = ("moving_average", "rsi_reversion", "breakout")
_CONTRACTS: Mapping[QueryId, ResultContract] = MappingProxyType(
    {
        QueryId.STRATEGY_RUN_COMPARISON: COMPARISON_CONTRACT,
        QueryId.TRADE_SEQUENCE: TRADE_SEQUENCE_CONTRACT,
        QueryId.EQUITY_DRAWDOWN_AUDIT: EQUITY_DRAWDOWN_AUDIT_CONTRACT,
        QueryId.STRATEGY_COHORT_SUMMARY: COHORT_SUMMARY_CONTRACT,
    }
)
Identifier: TypeAlias = str | int


@dataclass(frozen=True)
class BenchmarkConfig:
    """Bounded deterministic fixture and timing configuration."""

    seed: int
    run_count: int
    equity_points_per_run: int
    trades_per_run: int
    warmups: int
    repetitions: int

    def __post_init__(self) -> None:
        _validate_integer("seed", self.seed, minimum=0, maximum=(2**63) - 1)
        _validate_integer("run_count", self.run_count, minimum=1, maximum=_MAX_RUNS)
        _validate_integer(
            "equity_points_per_run",
            self.equity_points_per_run,
            minimum=1,
            maximum=_MAX_EQUITY_POINTS_PER_RUN,
        )
        _validate_integer(
            "trades_per_run",
            self.trades_per_run,
            minimum=1,
            maximum=_MAX_TRADES_PER_RUN,
        )
        _validate_integer("warmups", self.warmups, minimum=0, maximum=_MAX_WARMUPS)
        _validate_integer(
            "repetitions", self.repetitions, minimum=1, maximum=_MAX_REPETITIONS
        )
        estimated_rows = self.run_count * (
            1 + self.equity_points_per_run + self.trades_per_run + len(_METRIC_NAMES)
        )
        if estimated_rows > _MAX_TOTAL_ROWS:
            raise ValueError("benchmark fixture exceeds the maximum total row count")


@dataclass(frozen=True)
class PlanRow:
    """One raw four-column SQLite query-plan row."""

    node_id: int
    parent_id: int
    auxiliary: int
    detail: str

    @property
    def label(self) -> str:
        """Return a stable coarse label without discarding the raw planner detail."""
        normalized = self.detail.upper()
        if "USE TEMP B-TREE" in normalized:
            return "USE TEMP B-TREE"
        if "SEARCH" in normalized:
            return "SEARCH"
        if "SCAN" in normalized:
            return "SCAN"
        return "OTHER"


@dataclass(frozen=True)
class TimingSummary:
    """Raw monotonic samples and deterministic descriptive statistics."""

    samples_ns: tuple[int, ...]
    minimum_ns: int
    median_ns: float
    maximum_ns: int
    p95_ns: int


@dataclass(frozen=True)
class QueryMeasurement:
    """Planner, contract, result, and timing evidence for one query variant."""

    schema_variant: str
    query_id: QueryId
    sql_sha256: str
    params: Mapping[str, Scalar]
    result_row_count: int
    contract_valid: bool
    plan_rows: tuple[PlanRow, ...]
    timing: TimingSummary
    result_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True)
class SchemaVariantEvidence:
    """Observed schema identity, table counts, and named index inventory."""

    name: str
    alembic_revision: str
    table_row_counts: Mapping[str, int]
    indexes: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "table_row_counts", MappingProxyType(dict(self.table_row_counts))
        )
        object.__setattr__(self, "indexes", MappingProxyType(dict(self.indexes)))


@dataclass(frozen=True)
class FixtureManifest:
    """Observed deterministic fixture identity and cohort metadata."""

    seed: int
    symbol: str
    start_date: datetime
    end_date: datetime
    strategy_names: tuple[str, ...]
    selected_run_id: str
    table_row_counts: Mapping[str, int]
    primary_identifiers: Mapping[str, tuple[Identifier, ...]]
    value_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "table_row_counts", MappingProxyType(dict(self.table_row_counts))
        )
        object.__setattr__(
            self, "primary_identifiers", MappingProxyType(dict(self.primary_identifiers))
        )


@dataclass(frozen=True)
class BenchmarkReport:
    """Complete reproducible SQLite benchmark evidence."""

    schema_version: str
    generated_at: datetime
    config: BenchmarkConfig
    environment: Mapping[str, str]
    variants: tuple[SchemaVariantEvidence, ...]
    measurements: tuple[QueryMeasurement, ...]
    fixture: FixtureManifest | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class _RunFixture:
    run_id: str
    strategy_name: str
    final_equity: float
    maximum_drawdown: float
    winning_trades: int
    gross_profit: float
    gross_loss: float


class DeterministicFixtureGenerator:
    """Insert synthetic analytics rows with one injected local random generator."""

    def seed(self, engine: Engine, config: BenchmarkConfig) -> FixtureManifest:
        """Insert one fixture in bounded Core batches and return observed metadata."""
        generator = random.Random(config.seed)
        start_date = datetime(2022, 1, 1)
        end_date = datetime(2024, 12, 31)
        symbol = "SPY"
        run_ids = tuple(
            f"run-{uuid.UUID(int=generator.getrandbits(128))}" for _ in range(config.run_count)
        )
        run_rows = tuple(
            {
                "id": run_id,
                "strategy_name": _STRATEGY_NAMES[index % len(_STRATEGY_NAMES)],
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "params_json": json.dumps(
                    {"lookback": generator.randint(5, 80), "threshold": generator.random()},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "initial_capital": 100_000.0,
                "commission_pct": 0.001,
                "slippage_pct": 0.0005,
                "created_at": start_date + timedelta(seconds=index),
            }
            for index, run_id in enumerate(run_ids)
        )

        with engine.begin() as connection:
            _insert_batches(connection, cast(Table, BacktestRun.__table__), run_rows)
            for index, run_row in enumerate(run_rows):
                run_fixture = self._insert_run_children(
                    connection,
                    generator,
                    config,
                    run_id=str(run_row["id"]),
                    strategy_name=str(run_row["strategy_name"]),
                    start_date=start_date,
                    run_index=index,
                )
                self._insert_metrics(connection, config, run_fixture)

        table_row_counts = _table_counts(engine)
        primary_identifiers = _primary_identifiers(engine)
        return FixtureManifest(
            seed=config.seed,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            strategy_names=_STRATEGY_NAMES,
            selected_run_id=run_ids[0],
            table_row_counts=table_row_counts,
            primary_identifiers=primary_identifiers,
            value_sha256=_database_value_hash(engine),
        )

    def _insert_run_children(
        self,
        connection: Connection,
        generator: random.Random,
        config: BenchmarkConfig,
        *,
        run_id: str,
        strategy_name: str,
        start_date: datetime,
        run_index: int,
    ) -> _RunFixture:
        trade_rows: list[dict[str, object]] = []
        winning_trades = 0
        gross_profit = 0.0
        gross_loss = 0.0
        for trade_index in range(config.trades_per_run):
            entry_price = round(generator.uniform(80.0, 180.0), 6)
            pnl = round(generator.uniform(-250.0, 400.0), 6)
            if pnl > 0:
                winning_trades += 1
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)
            quantity = generator.randint(1, 100)
            exit_price = round(entry_price + (pnl / quantity), 6)
            entry_date = start_date + timedelta(days=trade_index * 2, minutes=run_index)
            trade_rows.append(
                {
                    "backtest_id": run_id,
                    "entry_date": entry_date,
                    "exit_date": entry_date + timedelta(days=1),
                    "direction": "LONG" if trade_index % 2 == 0 else "SHORT",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": quantity,
                    "pnl": pnl,
                    "pnl_pct": pnl / (entry_price * quantity),
                    "commission": round(entry_price * quantity * 0.001, 6),
                }
            )
        _insert_batches(connection, cast(Table, TradeRecord.__table__), trade_rows)

        equity_rows: list[dict[str, object]] = []
        equity = 100_000.0
        running_peak = equity
        maximum_drawdown = 0.0
        for point_index in range(config.equity_points_per_run):
            if point_index:
                equity = round(equity * (1.0 + generator.uniform(-0.002, 0.0025)), 6)
            running_peak = max(running_peak, equity)
            drawdown = (equity - running_peak) / running_peak
            maximum_drawdown = min(maximum_drawdown, drawdown)
            equity_rows.append(
                {
                    "backtest_id": run_id,
                    "date": start_date + timedelta(minutes=point_index),
                    "equity": equity,
                    "drawdown_pct": drawdown,
                }
            )
        _insert_batches(connection, cast(Table, EquityCurvePoint.__table__), equity_rows)
        return _RunFixture(
            run_id=run_id,
            strategy_name=strategy_name,
            final_equity=equity,
            maximum_drawdown=maximum_drawdown,
            winning_trades=winning_trades,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
        )

    def _insert_metrics(
        self, connection: Connection, config: BenchmarkConfig, fixture: _RunFixture
    ) -> None:
        total_return = (fixture.final_equity - 100_000.0) / 100_000.0
        sharpe = 0.5 + (_STRATEGY_NAMES.index(fixture.strategy_name) * 0.2)
        profit_factor = fixture.gross_profit / fixture.gross_loss if fixture.gross_loss else 1.0
        values = (
            sharpe,
            sharpe + 0.1,
            total_return,
            fixture.maximum_drawdown,
            float(config.equity_points_per_run),
            fixture.winning_trades / config.trades_per_run,
            profit_factor,
            total_return / abs(fixture.maximum_drawdown)
            if fixture.maximum_drawdown
            else 0.0,
            float(config.trades_per_run),
            total_return,
        )
        rows = tuple(
            {
                "backtest_id": fixture.run_id,
                "metric_name": name,
                "metric_value": value,
            }
            for name, value in zip(_METRIC_NAMES, values, strict=True)
        )
        _insert_batches(connection, cast(Table, MetricRecord.__table__), rows)


class BenchmarkRunner:
    """Create identical baseline/hardened SQLite copies and measure fixed queries."""

    def __init__(
        self,
        database_out: Path,
        *,
        clock: Callable[[], int] = time.perf_counter_ns,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_out = Path(database_out).expanduser().absolute()
        self._clock = clock
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self._catalogue = QueryCatalogue()

    def run(self, config: BenchmarkConfig) -> BenchmarkReport:
        """Return measured evidence and atomically publish the hardened database."""
        self._database_out.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self._database_out):
            raise FileExistsError("benchmark database destination already exists")

        with tempfile.TemporaryDirectory(
            dir=self._database_out.parent, prefix=".sql-benchmark-"
        ) as raw_directory:
            directory = Path(raw_directory)
            baseline_path = directory / "baseline.db"
            hardened_path = directory / "hardened.db"
            _upgrade(baseline_path, _BASELINE_REVISION)
            baseline_seed_engine = create_db_engine(_sqlite_url(baseline_path))
            try:
                fixture = DeterministicFixtureGenerator().seed(baseline_seed_engine, config)
            finally:
                baseline_seed_engine.dispose()

            _backup_database(baseline_path, hardened_path)
            _upgrade(hardened_path, "head")
            baseline_engine = create_db_engine(_sqlite_url(baseline_path))
            hardened_engine = create_db_engine(_sqlite_url(hardened_path))
            try:
                _analyze(baseline_engine)
                _analyze(hardened_engine)
                variants = (
                    _schema_evidence("baseline", baseline_engine),
                    _schema_evidence("hardened", hardened_engine),
                )
                matrix = _measurement_matrix(fixture)
                measurements = tuple(
                    self._measure_variant(variant_name, engine, query_id, params, config)
                    for variant_name, engine in (
                        ("baseline", baseline_engine),
                        ("hardened", hardened_engine),
                    )
                    for query_id, params in matrix
                )
                _require_identical_results(measurements)
            finally:
                baseline_engine.dispose()
                hardened_engine.dispose()

            hardened_path.replace(self._database_out)

        return BenchmarkReport(
            schema_version=_SCHEMA_VERSION,
            generated_at=self._utc_now(),
            config=config,
            environment=_environment(),
            variants=variants,
            measurements=measurements,
            fixture=fixture,
        )

    def _measure_variant(
        self,
        schema_variant: str,
        engine: Engine,
        query_id: QueryId,
        params: Mapping[str, Scalar],
        config: BenchmarkConfig,
    ) -> QueryMeasurement:
        frame = _execute_validated(engine, query_id, params, self._catalogue)
        loaded = self._catalogue.load(query_id)
        return QueryMeasurement(
            schema_variant=schema_variant,
            query_id=query_id,
            sql_sha256=loaded.sha256,
            params=params,
            result_row_count=len(frame.index),
            contract_valid=True,
            plan_rows=_capture_plan(engine, query_id, params, self._catalogue),
            timing=_measure_timing(
                lambda: _execute_validated(engine, query_id, params, self._catalogue),
                warmups=config.warmups,
                repetitions=config.repetitions,
                clock=self._clock,
            ),
            result_sha256=_frame_hash(frame),
        )


def _validate_integer(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")


def _insert_batches(
    connection: Connection, table: Table, rows: Sequence[Mapping[str, object]]
) -> None:
    for offset in range(0, len(rows), _BATCH_SIZE):
        batch = rows[offset : offset + _BATCH_SIZE]
        if batch:
            connection.execute(table.insert(), [dict(row) for row in batch])


def _table_counts(engine: Engine) -> Mapping[str, int]:
    with engine.connect() as connection:
        return MappingProxyType(
            {
                table_name: int(
                    connection.exec_driver_sql(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    ).scalar_one()
                )
                for table_name in _APPLICATION_TABLES
            }
        )


def _primary_identifiers(engine: Engine) -> Mapping[str, tuple[Identifier, ...]]:
    with engine.connect() as connection:
        return MappingProxyType(
            {
                table_name: tuple(
                    cast(Identifier, row[0])
                    for row in connection.exec_driver_sql(
                        f'SELECT id FROM "{table_name}" ORDER BY id'
                    )
                )
                for table_name in _APPLICATION_TABLES
            }
        )


def _database_value_hash(engine: Engine) -> str:
    values: dict[str, tuple[tuple[object, ...], ...]] = {}
    with engine.connect() as connection:
        for table_name in _APPLICATION_TABLES:
            values[table_name] = tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    f'SELECT * FROM "{table_name}" ORDER BY id'
                )
            )
    encoded = json.dumps(values, default=str, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _upgrade(path: Path, revision: str) -> None:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = _sqlite_url(path)
    command.upgrade(config, revision)


def _backup_database(source_path: Path, destination_path: Path) -> None:
    with (
        closing(sqlite3.connect(source_path)) as source,
        closing(sqlite3.connect(destination_path)) as destination,
    ):
        source.backup(destination)


def _analyze(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("ANALYZE")


def _schema_evidence(name: str, engine: Engine) -> SchemaVariantEvidence:
    inspector = inspect(engine)
    indexes = {
        table_name: tuple(
            sorted(
                str(item["name"])
                for item in inspector.get_indexes(table_name)
                if item.get("name") is not None
            )
        )
        for table_name in _APPLICATION_TABLES
    }
    with engine.connect() as connection:
        revision = str(
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        )
    return SchemaVariantEvidence(
        name=name,
        alembic_revision=revision,
        table_row_counts=_table_counts(engine),
        indexes=indexes,
    )


def _measurement_matrix(
    fixture: FixtureManifest,
) -> tuple[tuple[QueryId, Mapping[str, Scalar]], ...]:
    start = _sqlite_datetime_text(fixture.start_date)
    end = _sqlite_datetime_text(fixture.end_date)
    return (
        (
            QueryId.STRATEGY_RUN_COMPARISON,
            MappingProxyType(
                {
                    "symbol": fixture.symbol,
                    "start_date": start,
                    "end_date": end,
                    "strategy_name": None,
                }
            ),
        ),
        (
            QueryId.TRADE_SEQUENCE,
            MappingProxyType({"run_id": fixture.selected_run_id}),
        ),
        (
            QueryId.EQUITY_DRAWDOWN_AUDIT,
            MappingProxyType({"run_id": fixture.selected_run_id, "tolerance": 1e-12}),
        ),
        (
            QueryId.STRATEGY_COHORT_SUMMARY,
            MappingProxyType(
                {
                    "symbol": fixture.symbol,
                    "start_date": start,
                    "end_date": end,
                    "minimum_run_count": 1,
                }
            ),
        ),
    )


def _execute_validated(
    engine: Engine,
    query_id: QueryId,
    params: Mapping[str, Scalar],
    catalogue: QueryCatalogue,
) -> pd.DataFrame:
    raw = AnalyticsRepository(engine, catalogue).execute(
        query_id, cast(Mapping[str, BindValue], params)
    )
    return validate_frame(raw, _CONTRACTS[query_id])


def _capture_plan(
    engine: Engine,
    query_id: QueryId,
    params: Mapping[str, Scalar],
    catalogue: QueryCatalogue,
) -> tuple[PlanRow, ...]:
    loaded = catalogue.load(query_id)
    statement = text("EXPLAIN QUERY PLAN " + loaded.statement.text)
    with engine.connect() as connection:
        rows = connection.execute(statement, dict(params)).fetchall()
    return tuple(
        PlanRow(int(row[0]), int(row[1]), int(row[2]), str(row[3])) for row in rows
    )


def _measure_timing(
    execute: Callable[[], object],
    *,
    warmups: int,
    repetitions: int,
    clock: Callable[[], int],
) -> TimingSummary:
    _validate_integer("warmups", warmups, minimum=0, maximum=_MAX_WARMUPS)
    _validate_integer("repetitions", repetitions, minimum=1, maximum=_MAX_REPETITIONS)
    for _ in range(warmups):
        execute()
    samples: list[int] = []
    for _ in range(repetitions):
        started = clock()
        execute()
        elapsed = clock() - started
        if elapsed < 0:
            raise RuntimeError("monotonic benchmark clock moved backwards")
        samples.append(elapsed)
    ordered = sorted(samples)
    return TimingSummary(
        samples_ns=tuple(samples),
        minimum_ns=ordered[0],
        median_ns=float(statistics.median(ordered)),
        maximum_ns=ordered[-1],
        p95_ns=ordered[math.ceil(0.95 * len(ordered)) - 1],
    )


def _frame_hash(frame: pd.DataFrame) -> str:
    encoded = frame.to_json(
        orient="split", date_format="iso", date_unit="us", double_precision=15
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _require_identical_results(measurements: Iterable[QueryMeasurement]) -> None:
    grouped: dict[QueryId, list[QueryMeasurement]] = {}
    for measurement in measurements:
        grouped.setdefault(measurement.query_id, []).append(measurement)
    for query_id, pair in grouped.items():
        facts = {(item.result_row_count, item.result_sha256) for item in pair}
        if len(pair) != 2 or len(facts) != 1:
            raise RuntimeError(f"schema variants returned different results for {query_id.value}")


def _environment() -> Mapping[str, str]:
    return MappingProxyType(
        {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "sqlalchemy": sqlalchemy.__version__,
            "pandas": pd.__version__,
            "platform": platform.platform(),
            "implementation": sys.implementation.name,
        }
    )


def _sqlite_datetime_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _sqlite_url(path: Path) -> str:
    encoded_path = quote(str(path), safe="/")
    return f"sqlite:///file:{encoded_path}?uri=true"
