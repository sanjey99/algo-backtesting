"""Tests for database layer — Step 9."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.crud import (
    get_backtest_run,
    get_equity_curve,
    get_metrics,
    get_trades,
    list_backtest_runs,
    save_backtest_run,
)
from src.db.tables import Base


@pytest.fixture
def db_session():
    """In-memory SQLite session for tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


SAMPLE_RUN = dict(
    strategy_name="ma_crossover",
    symbol="SPY",
    start_date=datetime(2022, 1, 1),
    end_date=datetime(2023, 1, 1),
    params={"fast": 10, "slow": 50},
    initial_capital=100_000.0,
    commission_pct=0.001,
    slippage_pct=0.0005,
    trades=[
        {
            "entry_date": datetime(2022, 3, 1),
            "exit_date": datetime(2022, 4, 1),
            "direction": "LONG",
            "entry_price": 440.0,
            "exit_price": 460.0,
            "quantity": 100,
            "pnl": 2000.0,
            "pnl_pct": 0.045,
            "commission": 88.0,
        }
    ],
    equity_curve=[
        {"date": datetime(2022, 1, 3), "equity": 100_000.0, "drawdown_pct": 0.0},
        {"date": datetime(2022, 1, 4), "equity": 101_000.0, "drawdown_pct": 0.0},
    ],
    metrics={"sharpe_ratio": 1.24, "cagr": 0.087, "max_drawdown": -0.1532},
)


class TestCRUD:
    def test_save_and_retrieve_run(self, db_session) -> None:
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        run = get_backtest_run(db_session, run_id)
        assert run is not None
        assert run.strategy_name == "ma_crossover"
        assert run.symbol == "SPY"
        assert run.initial_capital == 100_000.0

    def test_save_returns_uuid(self, db_session) -> None:
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        assert len(run_id) > 0

    def test_custom_run_id(self, db_session) -> None:
        run_id = save_backtest_run(db_session, run_id="custom-id-123", **SAMPLE_RUN)
        db_session.commit()
        assert run_id == "custom-id-123"
        run = get_backtest_run(db_session, "custom-id-123")
        assert run is not None

    def test_trades_persisted(self, db_session) -> None:
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        trades = get_trades(db_session, run_id)
        assert len(trades) == 1
        assert trades[0].entry_price == 440.0
        assert trades[0].pnl == 2000.0

    def test_equity_curve_persisted(self, db_session) -> None:
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        curve = get_equity_curve(db_session, run_id)
        assert len(curve) == 2
        assert curve[0].equity == 100_000.0

    def test_metrics_persisted(self, db_session) -> None:
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        metrics = get_metrics(db_session, run_id)
        assert metrics["sharpe_ratio"] == pytest.approx(1.24)
        assert metrics["max_drawdown"] == pytest.approx(-0.1532)

    def test_list_backtest_runs(self, db_session) -> None:
        save_backtest_run(db_session, **SAMPLE_RUN)
        save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        runs = list_backtest_runs(db_session)
        assert len(runs) == 2

    def test_round_trip_metrics_identical(self, db_session) -> None:
        """Save and load produces identical metrics."""
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()

        loaded = get_metrics(db_session, run_id)
        for key, value in SAMPLE_RUN["metrics"].items():
            assert loaded[key] == pytest.approx(value)

    def test_get_nonexistent_run(self, db_session) -> None:
        result = get_backtest_run(db_session, "nonexistent-id")
        assert result is None

    def test_round_trip_across_sessions(self) -> None:
        """Data saved in one session is visible in a fresh session (R-17)."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine)

        # Write session
        session1 = SessionFactory()
        run_id = save_backtest_run(session1, **SAMPLE_RUN)
        session1.close()

        # Read session — completely fresh
        session2 = SessionFactory()
        run = get_backtest_run(session2, run_id)
        assert run is not None
        assert run.strategy_name == SAMPLE_RUN["strategy_name"]
        session2.close()
