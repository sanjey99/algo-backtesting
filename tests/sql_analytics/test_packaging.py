"""Installed-wheel smoke coverage for packaged SQL analytics resources."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command, cwd=cwd, env=env, check=False, text=True, capture_output=True
    )
    assert completed.returncode == 0, (
        f"command failed: {command!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def test_installed_wheel_migrates_and_executes_every_packaged_query(tmp_path: Path) -> None:
    """Omitting SQL or migration resources from the wheel must break the installed workflow."""
    wheel_directory = tmp_path / "wheel"
    wheel_directory.mkdir()
    _run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=PROJECT_ROOT,
    )
    wheels = tuple(wheel_directory.glob("*.whl"))
    assert len(wheels) == 1

    environment_directory = tmp_path / "environment"
    _run(
        [sys.executable, "-m", "venv", str(environment_directory)],
        cwd=tmp_path,
    )
    python = environment_directory / "bin" / "python"
    _run(
        ["uv", "pip", "install", "--python", str(python), str(wheels[0])],
        cwd=tmp_path,
    )

    database = tmp_path / "installed-wheel.db"
    smoke_script = f"""
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

import src
from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import (
    COHORT_SUMMARY_CONTRACT,
    COMPARISON_CONTRACT,
    DEFECT_RECORD_CONTRACT,
    DUPLICATE_CONTRACT,
    EQUITY_DRAWDOWN_AUDIT_CONTRACT,
    PER_RUN_RECONCILIATION_CONTRACT,
    TABLE_COUNTS_CONTRACT,
    TRADE_SEQUENCE_CONTRACT,
    QueryId,
)
from src.analytics.sql_service import AnalyticsRepository, validate_frame
from src.db.crud import save_backtest_run
from src.db.database import create_db_engine
from src.db.migrate import upgrade_database

database = Path({str(database)!r})
database_url = f"sqlite:///{{database}}"
assert Path(src.__file__).is_relative_to(Path({str(environment_directory)!r}))
outcome = upgrade_database(database_url)
assert outcome.current_revision == "20260804_01"

engine = create_db_engine(database_url)
try:
    with Session(engine) as session:
        save_backtest_run(
            session,
            run_id="wheel-run",
            strategy_name="moving_average",
            symbol="SPY",
            start_date=datetime(2024, 1, 1),
            end_date=datetime(2024, 1, 31),
            params={{"fast": 10, "slow": 30}},
            initial_capital=10_000.0,
            commission_pct=0.001,
            slippage_pct=0.0005,
            trades=[{{
                "entry_date": datetime(2024, 1, 2),
                "exit_date": datetime(2024, 1, 3),
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": 101.0,
                "quantity": 1,
                "pnl": 1.0,
                "pnl_pct": 0.01,
                "commission": 0.1,
            }}],
            equity_curve=[
                {{"date": datetime(2024, 1, 1), "equity": 10_000.0, "drawdown_pct": 0.0}},
                {{"date": datetime(2024, 1, 31), "equity": 10_100.0, "drawdown_pct": 0.0}},
            ],
            metrics={{
                "sharpe_ratio": 1.0,
                "sortino_ratio": 1.1,
                "cagr": 0.01,
                "max_drawdown": 0.0,
                "max_drawdown_duration": 0.0,
                "win_rate": 1.0,
                "profit_factor": 1.0,
                "calmar_ratio": 0.0,
                "total_trades": 1.0,
                "total_return": 0.01,
            }},
        )

    cases = (
        (QueryId.STRATEGY_RUN_COMPARISON, {{
            "symbol": "SPY",
            "start_date": "2024-01-01 00:00:00.000000",
            "end_date": "2024-01-31 00:00:00.000000",
            "strategy_name": None,
        }}, COMPARISON_CONTRACT),
        (QueryId.TRADE_SEQUENCE, {{"run_id": "wheel-run"}}, TRADE_SEQUENCE_CONTRACT),
        (QueryId.EQUITY_DRAWDOWN_AUDIT, {{
            "run_id": "wheel-run", "tolerance": 0.0,
        }}, EQUITY_DRAWDOWN_AUDIT_CONTRACT),
        (QueryId.STRATEGY_COHORT_SUMMARY, {{
            "symbol": "SPY",
            "start_date": "2024-01-01 00:00:00.000000",
            "end_date": "2024-01-31 00:00:00.000000",
            "minimum_run_count": 1,
        }}, COHORT_SUMMARY_CONTRACT),
        (QueryId.INTEGRITY_TABLE_COUNTS, {{}}, TABLE_COUNTS_CONTRACT),
        (QueryId.INTEGRITY_PER_RUN_RECONCILIATION, {{
            "scope_all": 1, "run_ids": ("",),
        }}, PER_RUN_RECONCILIATION_CONTRACT),
        (QueryId.INTEGRITY_DUPLICATE_METRICS, {{
            "scope_all": 1, "run_ids": ("",),
        }}, DUPLICATE_CONTRACT),
        (QueryId.INTEGRITY_DUPLICATE_EQUITY, {{
            "scope_all": 1, "run_ids": ("",),
        }}, DUPLICATE_CONTRACT),
        (QueryId.INTEGRITY_ORPHAN_CHILDREN, {{
            "scope_all": 1, "run_ids": ("",),
        }}, DEFECT_RECORD_CONTRACT),
        (QueryId.INTEGRITY_INVALID_RECORDS, {{
            "scope_all": 1, "run_ids": ("",),
        }}, DEFECT_RECORD_CONTRACT),
        (QueryId.INTEGRITY_METRIC_RECONCILIATION, {{
            "scope_all": 1, "run_ids": ("",), "tolerance": 0.02,
        }}, DEFECT_RECORD_CONTRACT),
    )
    repository = AnalyticsRepository(engine)
    catalogue = QueryCatalogue()
    assert {{query_id for query_id, _, _ in cases}} == set(QueryId)
    for query_id, params, contract in cases:
        loaded = catalogue.load(query_id)
        assert loaded.sha256
        frame = repository.execute(query_id, params)
        validated = validate_frame(frame, contract)
        assert tuple(validated.columns) == contract.names
finally:
    engine.dispose()
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    _run([str(python), "-c", smoke_script], cwd=tmp_path, env=environment)
