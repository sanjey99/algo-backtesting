"""Offline integration tests for the one-shot cloud backtest worker."""
from __future__ import annotations

import hashlib
import io
import json
import math
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.cloud.contracts import (
    DatasetRef,
    FailureCode,
    ResearchRequest,
    RunRecord,
    RunSpec,
    RunStatus,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.prepare_handler import _run_spec_payload
from src.cloud.storage import LifecycleClass, ObjectSizeLimitError, StateTransitionError
from src.engine.backtest import BacktestEngine, BacktestResult
from src.models.portfolio import EquityPoint
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "spy-daily.parquet"
FIXTURE_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "adj_close")
FIXTURE_SHA256 = "f9fa01d03f257c361de21c5cb77cb9ac7c4982783383b9e2be4039d6aeb20ace"
NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
RUN_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
IMAGE_DIGEST = "sha256:" + "c" * 64
EXPECTED_TRADE_SCHEMA = pa.schema(
    [
        pa.field("trade_id", pa.string()),
        pa.field("symbol", pa.string()),
        pa.field("direction", pa.string()),
        pa.field("entry_timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("exit_timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("entry_price", pa.float64()),
        pa.field("exit_price", pa.float64()),
        pa.field("quantity", pa.int64()),
        pa.field("commission", pa.float64()),
        pa.field("pnl", pa.float64()),
        pa.field("pnl_pct", pa.float64()),
    ]
)
EXPECTED_EQUITY_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("equity", pa.float64()),
        pa.field("drawdown_pct", pa.float64()),
    ]
)


def fixed_clock() -> datetime:
    return NOW


def dataset_ref(body: bytes) -> DatasetRef:
    return DatasetRef(
        bucket="research-artifacts",
        key="datasets/v1/acquisition-1/spy-daily.parquet",
        sha256=sha256_hex(body),
        manifest_key="datasets/v1/acquisition-1/manifest.json",
        manifest_sha256="b" * 64,
        symbol="SPY",
        calendar="XNYS",
        interval="1d",
        start=date(2024, 1, 2),
        end=date(2024, 3, 29),
        acquisition_id="acquisition-1",
        completed_at=NOW,
    )


def request(**changes: object) -> ResearchRequest:
    values: dict[str, object] = {
        "symbol": "SPY",
        "start": "2024-01-02",
        "end": "2024-03-29",
        "strategy_key": "ma_crossover",
        "strategy_parameters": {"fast_period": 3, "slow_period": 7},
        "initial_capital": 100_000.0,
        "commission_pct": 0.001,
        "slippage_pct": 0.0005,
        "visibility": "PRIVATE",
    }
    values.update(changes)
    return ResearchRequest.model_validate(values)


def seed_run(
    store: FakeObjectStore,
    repository: FakeRunRepository,
    *,
    dataset_body: bytes | None = None,
    raw_spec: bytes | None = None,
    request_changes: dict[str, object] | None = None,
    status: RunStatus = RunStatus.PENDING,
) -> RunSpec:
    body = FIXTURE_PATH.read_bytes() if dataset_body is None else dataset_body
    request_payload = request_changes if request_changes is not None else {}
    spec = RunSpec.create(
        request=request(**request_payload),
        dataset=dataset_ref(body),
        image_digest=IMAGE_DIGEST,
        now=NOW,
        run_id=RUN_ID,
    )
    store.put(spec.dataset.key, body, "application/vnd.apache.parquet")
    spec_body = canonical_json_bytes(_run_spec_payload(spec)) if raw_spec is None else raw_spec
    store.put(spec.run_spec_key, spec_body, "application/json")
    record = RunRecord(
        run_id=spec.run_id,
        status=RunStatus.PENDING,
        visibility=spec.request.visibility,
        dataset_key=spec.dataset.key,
        dataset_sha256=spec.dataset.sha256,
        run_spec_key=spec.run_spec_key,
        result_prefix=spec.result_prefix,
        image_digest=spec.image_digest,
        created_at=NOW,
        expires_at=int((NOW + timedelta(days=45)).timestamp()),
    )
    repository.create_pending(record)
    if status is RunStatus.RUNNING:
        repository.mark_running(spec.run_id, NOW)
    elif status is RunStatus.SUCCEEDED:
        repository.mark_running(spec.run_id, NOW)
        repository.mark_succeeded(spec.run_id, NOW)
    elif status is RunStatus.FAILED:
        repository.mark_failed(spec.run_id, FailureCode.WORKER_FAILED, NOW)
    return spec


def artifact_bodies(store: FakeObjectStore, spec: RunSpec) -> dict[str, bytes]:
    return {
        call.key.removeprefix(spec.result_prefix): call.body
        for call in store.put_calls
        if call.key.startswith(spec.result_prefix) and call.key != spec.run_spec_key
    }


def test_execute_run_admits_exact_inputs_runs_real_engine_and_publishes_manifest_last() -> None:
    from src.cloud.worker import execute_run

    fixture = FIXTURE_PATH.read_bytes()
    assert tuple(pd.read_parquet(io.BytesIO(fixture)).columns) == FIXTURE_COLUMNS
    assert hashlib.sha256(fixture).hexdigest() == FIXTURE_SHA256
    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository)

    manifest = execute_run(
        spec.run_spec_key,
        object_store=store,
        run_repository=repository,
        clock=fixed_clock,
    )

    assert [(call.run_id, call.started_at) for call in repository.mark_running_calls] == [
        (spec.run_id, NOW)
    ]
    assert repository.mark_succeeded_calls == ()
    assert repository.mark_failed_calls == ()
    assert [call.key for call in store.put_calls][-5:] == [
        f"{spec.result_prefix}result.json",
        f"{spec.result_prefix}trades.parquet",
        f"{spec.result_prefix}equity-curve.parquet",
        f"{spec.result_prefix}report.html",
        f"{spec.result_prefix}checksums.json",
    ]
    assert all(
        call.lifecycle_class is LifecycleClass.TRANSIENT
        for call in store.put_calls
        if call.key.startswith(spec.result_prefix) and call.key != spec.run_spec_key
    )
    artifacts = artifact_bodies(store, spec)
    summary = json.loads(artifacts["result.json"])
    assert set(summary) == {
        "schema_version", "run_id", "symbol", "start_date", "end_date", "strategy_name",
        "strategy_parameters", "initial_capital", "final_equity", "metrics", "total_trades",
        "image_digest", "dataset_sha256", "completed_at",
    }
    assert summary["run_id"] == spec.run_id
    assert summary["dataset_sha256"] == spec.dataset.sha256
    assert summary["start_date"] == "2024-01-02"
    assert summary["end_date"] == "2024-03-29"
    assert not {"bars", "trades", "equity_curve"} & summary.keys()
    assert pd.read_parquet(io.BytesIO(artifacts["trades.parquet"])).columns.tolist() == [
        "trade_id", "symbol", "direction", "entry_timestamp", "exit_timestamp", "entry_price",
        "exit_price", "quantity", "commission", "pnl", "pnl_pct",
    ]
    assert pd.read_parquet(io.BytesIO(artifacts["equity-curve.parquet"])).columns.tolist() == [
        "timestamp", "equity", "drawdown_pct"
    ]
    assert pq.ParquetFile(io.BytesIO(artifacts["trades.parquet"])).schema_arrow == (
        EXPECTED_TRADE_SCHEMA
    )
    assert pq.ParquetFile(io.BytesIO(artifacts["equity-curve.parquet"])).schema_arrow == (
        EXPECTED_EQUITY_SCHEMA
    )
    assert 'id="cloud-run-123e4567-e89b-12d3-a456-426614174000"' in (
        artifacts["report.html"].decode()
    )
    assert {entry.name for entry in manifest.artifacts} == {
        "run-spec.json", "result.json", "trades.parquet", "equity-curve.parquet", "report.html",
    }
    run_spec_entry = next(entry for entry in manifest.artifacts if entry.name == "run-spec.json")
    assert run_spec_entry.sha256 == sha256_hex(store.get(spec.run_spec_key, 1_000_000))


def test_execute_run_publishes_public_artifacts_with_selected_public_lifecycle() -> None:
    from src.cloud.worker import execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(
        store,
        repository,
        request_changes={"visibility": "PUBLIC"},
    )

    execute_run(
        spec.run_spec_key,
        object_store=store,
        run_repository=repository,
        clock=fixed_clock,
    )

    assert all(
        call.lifecycle_class is LifecycleClass.SELECTED_PUBLIC
        for call in store.put_calls
        if call.key.startswith(spec.result_prefix) and call.key != spec.run_spec_key
    )


def test_execute_run_rejects_dataset_checksum_before_engine_or_artifacts() -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository, dataset_body=b"different-but-hashed-in-spec")
    tampered_key = spec.dataset.key
    store._objects[tampered_key] = b"tampered"

    with pytest.raises(WorkerError, match="dataset digest mismatch"):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert [(call.run_id, call.started_at) for call in repository.mark_running_calls] == [
        (spec.run_id, NOW)
    ]
    assert artifact_bodies(store, spec) == {}


@pytest.mark.parametrize(
    ("component", "value"),
    [
        pytest.param("dataset", "QQQ", id="symbol"),
        pytest.param("request", "2024-03-28", id="range"),
    ],
)
def test_execute_run_rejects_dataset_request_identity_mismatch_before_running(
    component: str,
    value: str,
) -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository)
    payload = json.loads(store.get(spec.run_spec_key, 1_000_000))
    if component == "dataset":
        payload["dataset"]["symbol"] = value
    else:
        payload["request"]["end"] = value
    store._objects[spec.run_spec_key] = canonical_json_bytes(payload)

    with pytest.raises(WorkerError, match="dataset and request do not agree"):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert repository.mark_running_calls == ()
    assert artifact_bodies(store, spec) == {}


def test_execute_run_rejects_out_of_range_candle_before_engine_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.cloud import worker

    store = FakeObjectStore()
    repository = FakeRunRepository()
    frame = pd.read_parquet(FIXTURE_PATH)
    out_of_range = frame.iloc[[-1]].assign(timestamp=pd.Timestamp("2024-04-01"))
    body_buffer = io.BytesIO()
    pd.concat([frame, out_of_range], ignore_index=True).to_parquet(body_buffer, index=False)
    spec = seed_run(store, repository, dataset_body=body_buffer.getvalue())
    engine_called = False

    def fail_if_called(*args: object, **kwargs: object) -> BacktestResult:
        nonlocal engine_called
        del args, kwargs
        engine_called = True
        raise AssertionError("engine must not receive an out-of-range candle")

    monkeypatch.setattr(BacktestEngine, "run", fail_if_called)

    with pytest.raises(worker.WorkerError, match="dataset contains an out-of-range candle"):
        worker.execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert engine_called is False
    assert artifact_bodies(store, spec) == {}


def _empty_result() -> BacktestResult:
    return BacktestResult(
        strategy_name="ma_crossover",
        symbol="SPY",
        start_date=NOW,
        end_date=NOW,
        parameters={"fast_period": 3, "slow_period": 7},
        trades=[],
        equity_curve=[],
        final_equity=100_000.0,
        initial_capital=100_000.0,
        run_id=str(RUN_ID),
    )


def test_empty_artifact_parquet_schemas_are_typed_and_deterministic() -> None:
    from src.cloud.worker import _parquet_bytes, equity_frame, trade_frame

    result = _empty_result()
    empty_trade_bytes = _parquet_bytes(trade_frame(result))
    empty_equity_bytes = _parquet_bytes(equity_frame(result))
    assert pq.ParquetFile(io.BytesIO(empty_trade_bytes)).schema_arrow == EXPECTED_TRADE_SCHEMA
    assert pq.ParquetFile(io.BytesIO(empty_equity_bytes)).schema_arrow == EXPECTED_EQUITY_SCHEMA
    assert empty_trade_bytes == _parquet_bytes(trade_frame(_empty_result()))
    assert empty_equity_bytes == _parquet_bytes(equity_frame(_empty_result()))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"not":"a run spec"}',
        canonical_json_bytes({"run_id": str(RUN_ID), "schema_version": "1"}),
    ],
)
def test_execute_run_rejects_noncanonical_or_malformed_run_spec_before_state_change(
    payload: bytes,
) -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository, raw_spec=payload)

    with pytest.raises(WorkerError, match="run specification admission failed"):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert repository.mark_running_calls == ()
    assert artifact_bodies(store, spec) == {}


def test_execute_run_rejects_key_that_does_not_match_admitted_run_spec() -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository)

    with pytest.raises(WorkerError, match="run specification key mismatch"):
        execute_run(
            "runs/v1/123e4567-e89b-12d3-a456-426614174000/not-run-spec.json",
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert repository.mark_running_calls == ()
    assert artifact_bodies(store, spec) == {}


@pytest.mark.parametrize(
    "strategy_changes",
    [
        {"strategy_key": "missing"},
        {"strategy_parameters": {"fast_period": 9, "slow_period": 7}},
    ],
)
def test_execute_run_rejects_unknown_or_bad_strategy_during_admission(
    strategy_changes: dict[str, object],
) -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository)
    payload = json.loads(store.get(spec.run_spec_key, 1_000_000))
    payload["request"].update(strategy_changes)
    store._objects[spec.run_spec_key] = canonical_json_bytes(payload)

    with pytest.raises(WorkerError, match="run specification admission failed"):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert repository.mark_running_calls == ()
    assert artifact_bodies(store, spec) == {}


@pytest.mark.parametrize(
    ("dataset_body", "is_oversized"),
    [
        pytest.param(b"not a parquet file", False, id="malformed-parquet"),
        pytest.param(b"x" * (64 * 1024 * 1024 + 1), True, id="oversized"),
    ],
)
def test_execute_run_rejects_malformed_or_oversized_dataset(
    dataset_body: bytes, is_oversized: bool
) -> None:
    from src.cloud.worker import WorkerError, execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository, dataset_body=dataset_body)

    expected = ObjectSizeLimitError if is_oversized else WorkerError
    with pytest.raises(expected):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert [(call.run_id, call.started_at) for call in repository.mark_running_calls] == [
        (spec.run_id, NOW)
    ]
    assert artifact_bodies(store, spec) == {}


def test_execute_run_rejects_terminal_state_before_paid_execution() -> None:
    from src.cloud.worker import execute_run

    store = FakeObjectStore()
    repository = FakeRunRepository()
    spec = seed_run(store, repository, status=RunStatus.SUCCEEDED)

    with pytest.raises(StateTransitionError):
        execute_run(
            spec.run_spec_key,
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )

    assert artifact_bodies(store, spec) == {}


def test_result_summary_converts_nonfinite_metrics_to_json_null() -> None:
    from src.cloud.worker import result_summary

    result = BacktestResult(
        strategy_name="ma_crossover",
        symbol="SPY",
        start_date=NOW,
        end_date=NOW,
        parameters={"fast_period": 3, "slow_period": 7},
        trades=[],
        equity_curve=[EquityPoint(NOW, 100_000.0)],
        final_equity=100_000.0,
        initial_capital=100_000.0,
        run_id=str(RUN_ID),
    )

    summary = result_summary(result, {"profit_factor": math.inf, "sharpe_ratio": 1.25})

    assert summary["metrics"] == {"profit_factor": None, "sharpe_ratio": 1.25}
    assert json.dumps(summary, allow_nan=False)


def test_execute_run_serializes_identical_artifacts_with_a_fixed_clock() -> None:
    from src.cloud.worker import execute_run

    first_store, first_repository = FakeObjectStore(), FakeRunRepository()
    first_spec = seed_run(first_store, first_repository)
    execute_run(
        first_spec.run_spec_key,
        object_store=first_store,
        run_repository=first_repository,
        clock=fixed_clock,
    )
    second_store, second_repository = FakeObjectStore(), FakeRunRepository()
    second_spec = seed_run(second_store, second_repository)
    execute_run(
        second_spec.run_spec_key,
        object_store=second_store,
        run_repository=second_repository,
        clock=fixed_clock,
    )

    assert artifact_bodies(first_store, first_spec) == artifact_bodies(second_store, second_spec)


class RecordingBoto3(ModuleType):
    def __init__(self) -> None:
        super().__init__("boto3")
        self.client_names: list[str] = []
        self.table_names: list[str] = []

    def client(self, name: str) -> object:
        self.client_names.append(name)
        return object()

    def resource(self, name: str) -> RecordingBoto3:
        assert name == "dynamodb"
        return self

    def Table(self, name: str) -> object:  # noqa: N802
        self.table_names.append(name)
        return object()


def test_main_requires_exact_argument_and_returns_nonzero_for_closed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.cloud import worker

    with pytest.raises(SystemExit, match="2"):
        worker.main([])

    fake_boto3 = RecordingBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("ARTIFACT_BUCKET", "research-artifacts")
    monkeypatch.setenv("RUN_TABLE", "research-runs")
    def fail_run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("secret-token")

    monkeypatch.setattr(worker, "execute_run", fail_run)

    assert worker.main(
        ["--run-spec-key", "runs/v1/123e4567-e89b-12d3-a456-426614174000/run-spec.json"]
    ) == 1
    assert fake_boto3.client_names == ["s3"]
    assert fake_boto3.table_names == ["research-runs"]
