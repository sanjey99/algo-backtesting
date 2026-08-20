"""Tests for FastAPI endpoints — Step 10.

Uses TestClient with an in-memory SQLite DB and patched data fetching.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.api import main as api_main
from src.api.deps import get_db
from src.api.main import app
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
)
from src.db import database
from src.db.database import create_db_engine
from src.db.migrate import SchemaNotCurrentError
from src.db.tables import Base
from src.models.candle import Candle

# ---------------------------------------------------------------------------
# In-memory DB override — StaticPool ensures all connections share one DB
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite:///:memory:"
_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def override_get_db() -> Generator[Session, None, None]:
    db = _TestSession()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


Base.metadata.create_all(bind=_engine)
app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="module", autouse=True)
def _dispose_test_engine() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.pop(get_db, None)
    _engine.dispose()


# ---------------------------------------------------------------------------
# Fixture: synthetic candles
# ---------------------------------------------------------------------------

def _make_candles(n: int = 150) -> list[Candle]:
    import random
    rng = random.Random(42)
    candles = []
    price = 100.0
    start = datetime(2020, 1, 2)
    for i in range(n):
        price *= 1 + rng.gauss(0.001, 0.015)
        candles.append(Candle(
            timestamp=start + timedelta(days=i),
            open=price * 0.999,
            high=price * 1.005,
            low=price * 0.994,
            close=price,
            volume=1_000_000.0,
            adj_close=price,
        ))
    return candles


FAKE_CANDLES = _make_candles(150)


def _fake_acquisition_result() -> AcquisitionResult:
    now = datetime(2024, 1, 1, tzinfo=UTC)
    request = AcquisitionRequest("SPY", date(2020, 1, 1), date(2022, 12, 31))
    return AcquisitionResult(
        pd.DataFrame(),
        AcquisitionManifest(
            "legacy-api-test",
            request,
            AcquisitionStatus.SUCCESS,
            counters={
                "expected_sessions": len(FAKE_CANDLES),
                "accepted_expected_sessions": len(FAKE_CANDLES),
                "missing_sessions": 0,
            },
            coverage=1.0,
            started_at=now,
            completed_at=now,
        ),
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with patch("src.api.main.init_db"):
        with TestClient(app) as c:
            yield c


def test_lifespan_rejects_unmigrated_real_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Application startup must verify, never silently migrate, a real database."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'unmigrated.db'}")
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    with pytest.raises(SchemaNotCurrentError):
        with TestClient(app):
            pass

    engine.dispose()


async def test_lifespan_configures_logging_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API process opts into structured logging only when it starts."""
    calls: list[None] = []
    monkeypatch.setattr(api_main, "configure_logging", lambda: calls.append(None))

    with patch("src.api.main.init_db"):
        async with api_main.lifespan(app):
            pass

    assert calls == [None]


async def test_lifespan_disposes_database_engine_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application shutdown must release connections owned by its global engine."""
    engine = create_db_engine("sqlite:///:memory:")
    closed_connections: list[object] = []
    event.listen(
        engine,
        "close",
        lambda dbapi_connection, _: closed_connections.append(dbapi_connection),
    )
    with engine.connect():
        pass
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    with patch("src.api.main.init_db"):
        async with api_main.lifespan(app):
            assert closed_connections == []

    assert len(closed_connections) == 1


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /api/strategies
# ---------------------------------------------------------------------------

class TestStrategies:
    def test_lists_strategies(self, client: TestClient) -> None:
        r = client.get("/api/strategies")
        assert r.status_code == 200
        keys = {s["key"] for s in r.json()}
        assert "ma_crossover" in keys
        assert "rsi_mean_reversion" in keys
        assert "breakout" in keys

    def test_strategy_has_parameter_space(self, client: TestClient) -> None:
        r = client.get("/api/strategies")
        for strat in r.json():
            assert "parameter_space" in strat
            assert isinstance(strat["parameter_space"], dict)


# ---------------------------------------------------------------------------
# POST /api/backtest
# ---------------------------------------------------------------------------

PATCH_TARGET = "src.api.routes.backtest._fetch_candles"


class TestRunBacktest:
    def test_returns_and_persists_the_requested_symbol(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            r = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "AAPL",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "params": {"fast_period": 5, "slow_period": 20},
            })
        assert r.status_code == 201
        body = r.json()
        assert "run_id" in body
        assert body["strategy_name"] == "ma_crossover"
        assert body["symbol"] == "AAPL"

        persisted = client.get(f"/api/backtest/{body['run_id']}")
        assert persisted.status_code == 200
        assert persisted.json()["symbol"] == "AAPL"

    def test_metrics_present(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            r = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
            })
        assert r.status_code == 201
        assert "sharpe_ratio" in r.json()["metrics"]

    def test_invalid_strategy_returns_400(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            r = client.post("/api/backtest", json={
                "strategy": "does_not_exist",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
            })
        assert r.status_code == 400

    def test_empty_candles_returns_422(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=[]):
            r = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "FAKE",
                "start": "2020-01-01",
                "end": "2022-12-31",
            })
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/backtest/{run_id}
# ---------------------------------------------------------------------------

class TestGetBacktest:
    def test_persists_entry_on_bar_after_signal(self, client: TestClient) -> None:
        candles = [
            Candle(
                timestamp=datetime(2020, 1, 2) + timedelta(days=index),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1_000_000.0,
                adj_close=price,
            )
            for index, price in enumerate(
                [100.0, 100.0, 100.0, 90.0, 100.0, 110.0, 115.0, 80.0, 75.0]
            )
        ]

        with patch(PATCH_TARGET, return_value=candles):
            post = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "params": {"fast_period": 2, "slow_period": 3},
            })

        assert post.status_code == 201
        trades = client.get(f"/api/backtest/{post.json()['run_id']}/trades")
        signal_candle = candles[5]
        expected_entry_candle = candles[6]

        assert trades.status_code == 200
        assert len(trades.json()) == 1
        entry_date = datetime.fromisoformat(trades.json()[0]["entry_date"])
        assert entry_date == expected_entry_candle.timestamp
        assert entry_date != signal_candle.timestamp

    def test_get_existing_run(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            post = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
            })
        run_id = post.json()["run_id"]

        r = client.get(f"/api/backtest/{run_id}")
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id

    def test_get_nonexistent_run_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/backtest/nonexistent-id")
        assert r.status_code == 404

    def test_get_trades_endpoint(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            post = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "params": {"fast_period": 5, "slow_period": 20},
            })
        run_id = post.json()["run_id"]

        r = client.get(f"/api/backtest/{run_id}/trades")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_equity_curve_endpoint(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            post = client.post("/api/backtest", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "params": {"fast_period": 5, "slow_period": 20},
            })
        run_id = post.json()["run_id"]

        r = client.get(f"/api/backtest/{run_id}/equity-curve")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        if data:
            assert "equity" in data[0]
            assert "drawdown_pct" in data[0]


# ---------------------------------------------------------------------------
# GET /api/backtest (list)
# ---------------------------------------------------------------------------

class TestListBacktests:
    def test_list_returns_array(self, client: TestClient) -> None:
        r = client.get("/api/backtest")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# POST /api/backtest/permutation-test (202 + poll)
# ---------------------------------------------------------------------------

class TestPermutationEndpoint:
    def test_rejects_unsupported_metric_before_accepting_job(
        self, client: TestClient
    ) -> None:
        with patch("src.api.routes.backtest._run_permutation_bg"):
            response = client.post("/api/backtest/permutation-test", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "n_permutations": 1,
                "metric": "sharp_ratio_typo",
            })

        assert response.status_code == 422

    def test_returns_202_and_job_id(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            r = client.post("/api/backtest/permutation-test", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "n_permutations": 5,
            })
        assert r.status_code == 202
        assert "job_id" in r.json()

    def test_poll_job(self, client: TestClient) -> None:
        with patch(PATCH_TARGET, return_value=FAKE_CANDLES):
            r = client.post("/api/backtest/permutation-test", json={
                "strategy": "ma_crossover",
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "n_permutations": 5,
            })
        job_id = r.json()["job_id"]
        poll = client.get(f"/api/backtest/permutation-test/{job_id}")
        assert poll.status_code == 200
        assert poll.json()["job_id"] == job_id

    def test_poll_missing_job_404(self, client: TestClient) -> None:
        r = client.get("/api/backtest/permutation-test/no-such-job")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/data/fetch
# ---------------------------------------------------------------------------

class TestDataFetch:
    def test_fetch_returns_candle_count(self, client: TestClient) -> None:
        # Patch df_to_candles so we don't need a real DataFrame
        with patch(
            "src.api.routes.data.acquire_result", return_value=_fake_acquisition_result()
        ), patch("src.api.routes.data.df_to_candles", return_value=FAKE_CANDLES):
            r = client.post("/api/data/fetch", json={
                "symbol": "SPY",
                "start": "2020-01-01",
                "end": "2022-12-31",
                "use_cache": False,
            })
        assert r.status_code == 200
        assert r.json()["n_candles"] == len(FAKE_CANDLES)

    def test_fetch_empty_returns_422(self, client: TestClient) -> None:
        with patch(
            "src.api.routes.data.acquire_result", return_value=_fake_acquisition_result()
        ), patch("src.api.routes.data.df_to_candles", return_value=[]):
            r = client.post("/api/data/fetch", json={
                "symbol": "FAKE",
                "start": "2020-01-01",
                "end": "2020-01-05",
                "use_cache": False,
            })
        assert r.status_code == 422
