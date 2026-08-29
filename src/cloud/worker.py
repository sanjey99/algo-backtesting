"""Execute one admitted cloud backtest and publish its immutable artifacts."""
from __future__ import annotations

import argparse
import io
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from src.analytics.metrics import compute_all_metrics
from src.analytics.report import generate_html_report
from src.cloud.contracts import (
    ArtifactDigest,
    ChecksumsManifest,
    RunSpec,
    Visibility,
    _parse_utc_datetime,
    _validate_bucket,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.storage import (
    DynamoRunRepository,
    LifecycleClass,
    ObjectStore,
    RunRepository,
    S3ObjectStore,
)
from src.data import df_to_candles
from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.models.candle import Candle
from src.models.trade import Trade
from src.observability import configure_logging, log_event
from src.strategies import STRATEGY_REGISTRY

logger = logging.getLogger(__name__)

_MAXIMUM_RUN_SPEC_BYTES = 128 * 1024
_MAXIMUM_DATASET_BYTES = 64 * 1024 * 1024
_TRADE_COLUMNS = (
    "trade_id",
    "symbol",
    "direction",
    "entry_timestamp",
    "exit_timestamp",
    "entry_price",
    "exit_price",
    "quantity",
    "commission",
    "pnl",
    "pnl_pct",
)
_EQUITY_COLUMNS = ("timestamp", "equity", "drawdown_pct")
_TRADE_DTYPES = {
    "trade_id": "string",
    "symbol": "string",
    "direction": "string",
    "entry_timestamp": "datetime64[ns, UTC]",
    "exit_timestamp": "datetime64[ns, UTC]",
    "entry_price": "float64",
    "exit_price": "float64",
    "quantity": "int64",
    "commission": "float64",
    "pnl": "float64",
    "pnl_pct": "float64",
}
_EQUITY_DTYPES = {
    "timestamp": "datetime64[ns, UTC]",
    "equity": "float64",
    "drawdown_pct": "float64",
}
_TRADE_ARROW_SCHEMA = pa.schema(
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
_EQUITY_ARROW_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("ns", tz="UTC")),
        pa.field("equity", pa.float64()),
        pa.field("drawdown_pct", pa.float64()),
    ]
)

Clock = Callable[[], datetime]


class WorkerError(Exception):
    """A closed worker-boundary failure suitable for workflow handling."""


def _utc_text(value: datetime) -> str:
    return _parse_utc_datetime(value).isoformat().replace("+00:00", "Z")


def _admit_run_spec_key(key: str) -> str:
    """Require the only derived run-spec location this worker may consume."""
    if not isinstance(key, str):
        raise WorkerError("run specification key mismatch")
    parts = key.split("/")
    if len(parts) != 4 or parts[0:2] != ["runs", "v1"] or parts[3] != "run-spec.json":
        raise WorkerError("run specification key mismatch")
    try:
        run_id = UUID(parts[2])
    except (ValueError, AttributeError):
        raise WorkerError("run specification key mismatch") from None
    if str(run_id) != parts[2]:
        raise WorkerError("run specification key mismatch")
    return key


def _request_payload(run_spec: RunSpec) -> dict[str, object]:
    request = run_spec.request
    return {
        "schema_version": request.schema_version,
        "symbol": request.symbol,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "strategy_key": request.strategy_key,
        "strategy_parameters": dict(request.strategy_parameters),
        "initial_capital": request.initial_capital,
        "commission_pct": request.commission_pct,
        "slippage_pct": request.slippage_pct,
        "visibility": request.visibility.value,
    }


def _dataset_payload(run_spec: RunSpec) -> dict[str, object]:
    dataset = run_spec.dataset
    return {
        "schema_version": dataset.schema_version,
        "bucket": dataset.bucket,
        "key": dataset.key,
        "sha256": dataset.sha256,
        "manifest_key": dataset.manifest_key,
        "manifest_sha256": dataset.manifest_sha256,
        "symbol": dataset.symbol,
        "calendar": dataset.calendar,
        "interval": dataset.interval,
        "start": dataset.start.isoformat(),
        "end": dataset.end.isoformat(),
        "acquisition_id": dataset.acquisition_id,
        "completed_at": _utc_text(dataset.completed_at),
    }


def _canonical_run_spec_bytes(run_spec: RunSpec) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": run_spec.schema_version,
            "run_id": run_spec.run_id,
            "dataset": _dataset_payload(run_spec),
            "request": _request_payload(run_spec),
            "image_digest": run_spec.image_digest,
            "created_at": _utc_text(run_spec.created_at),
            "maximum_runtime_seconds": run_spec.maximum_runtime_seconds,
            "run_spec_key": run_spec.run_spec_key,
            "result_prefix": run_spec.result_prefix,
        }
    )


def _admit_run_spec(run_spec_key: str, body: bytes) -> RunSpec:
    try:
        run_spec = RunSpec.model_validate_json(body)
    except (ValidationError, ValueError, TypeError, UnicodeDecodeError):
        raise WorkerError("run specification admission failed") from None
    if run_spec.run_spec_key != run_spec_key:
        raise WorkerError("run specification key mismatch")
    if body != _canonical_run_spec_bytes(run_spec):
        raise WorkerError("run specification admission failed")
    if (
        run_spec.dataset.symbol != run_spec.request.symbol
        or run_spec.dataset.start != run_spec.request.start
        or run_spec.dataset.end != run_spec.request.end
    ):
        raise WorkerError("dataset and request do not agree")
    return run_spec


def _stable_trade_id(run_id: str, position: int, trade: Trade) -> str:
    exit_timestamp = "" if trade.exit_date is None else _timestamp_text(trade.exit_date)
    exit_price = "" if trade.exit_price is None else format(trade.exit_price, ".17g")
    identity = "|".join(
        (
            run_id,
            str(position),
            trade.symbol,
            trade.direction.value,
            _timestamp_text(trade.entry_date),
            format(trade.entry_price, ".17g"),
            str(trade.quantity),
            exit_timestamp,
            exit_price,
            format(trade.commission, ".17g"),
        )
    )
    return sha256_hex(identity.encode("utf-8"))


def _utc_timestamp(value: datetime) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize(UTC) if timestamp.tzinfo is None else timestamp.tz_convert(UTC)


def _timestamp_text(value: datetime) -> str:
    return _utc_timestamp(value).isoformat().replace("+00:00", "Z")


def _typed_frame(
    rows: list[dict[str, object]], columns: tuple[str, ...], dtypes: Mapping[str, str]
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            column: pd.Series([row[column] for row in rows], dtype=dtypes[column])
            for column in columns
        }
    )


def trade_frame(result: BacktestResult) -> pd.DataFrame:
    """Return completed trade artifacts with cloud-run-derived stable IDs."""
    rows: list[dict[str, object]] = []
    for position, trade in enumerate(result.trades):
        rows.append(
            {
                "trade_id": _stable_trade_id(result.run_id, position, trade),
                "symbol": trade.symbol,
                "direction": trade.direction.value,
                "entry_timestamp": _utc_timestamp(trade.entry_date),
                "exit_timestamp": (
                    None if trade.exit_date is None else _utc_timestamp(trade.exit_date)
                ),
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "quantity": trade.quantity,
                "commission": trade.commission,
                "pnl": trade.pnl,
                "pnl_pct": trade.pnl_pct,
            }
        )
    return _typed_frame(rows, _TRADE_COLUMNS, _TRADE_DTYPES)


def equity_frame(result: BacktestResult) -> pd.DataFrame:
    """Return UTC equity observations with an explicit schema even when empty."""
    rows = [
        {
            "timestamp": _utc_timestamp(point.date),
            "equity": point.equity,
            "drawdown_pct": point.drawdown_pct,
        }
        for point in result.equity_curve
    ]
    return _typed_frame(rows, _EQUITY_COLUMNS, _EQUITY_DTYPES)


def _require_candle_range(run_spec: RunSpec, candles: list[Candle]) -> None:
    for candle in candles:
        candle_date = candle.timestamp.date()
        if not run_spec.dataset.start <= candle_date <= run_spec.dataset.end:
            raise WorkerError("dataset contains an out-of-range candle")


def _json_metric(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def result_summary(result: BacktestResult, metrics: Mapping[str, float]) -> dict[str, object]:
    """Return only the bounded public portion of a backtest result."""
    return {
        "run_id": result.run_id,
        "symbol": result.symbol,
        "start_date": result.start_date.date().isoformat(),
        "end_date": result.end_date.date().isoformat(),
        "strategy_name": result.strategy_name,
        "strategy_parameters": dict(result.parameters),
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "metrics": {name: _json_metric(value) for name, value in metrics.items()},
        "total_trades": sum(1 for trade in result.trades if trade.is_closed),
    }


def _parquet_bytes(frame: pd.DataFrame) -> bytes:
    columns = tuple(frame.columns)
    if columns == _TRADE_COLUMNS:
        schema = _TRADE_ARROW_SCHEMA
    elif columns == _EQUITY_COLUMNS:
        schema = _EQUITY_ARROW_SCHEMA
    else:
        raise ValueError("unsupported artifact parquet schema")
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False), buffer)
    return buffer.getvalue()


def _artifact_digest(name: str, body: bytes) -> ArtifactDigest:
    return ArtifactDigest(name=name, byte_length=len(body), sha256=sha256_hex(body))


def execute_run(
    run_spec_key: str,
    *,
    object_store: ObjectStore,
    run_repository: RunRepository,
    clock: Clock,
) -> ChecksumsManifest:
    """Run one admitted specification and publish its checksums manifest last."""
    admitted_key = _admit_run_spec_key(run_spec_key)
    try:
        run_spec_body = object_store.get(admitted_key, _MAXIMUM_RUN_SPEC_BYTES)
    except Exception:
        raise WorkerError("run specification download failed") from None
    run_spec = _admit_run_spec(admitted_key, run_spec_body)

    run_repository.mark_running(run_spec.run_id, _parse_utc_datetime(clock()))
    dataset_body = object_store.get(run_spec.dataset.key, _MAXIMUM_DATASET_BYTES)
    if sha256_hex(dataset_body) != run_spec.dataset.sha256:
        raise WorkerError("dataset digest mismatch")
    try:
        dataset_frame = pd.read_parquet(io.BytesIO(dataset_body))
        candles = df_to_candles(dataset_frame)
    except Exception:
        raise WorkerError("dataset parquet is invalid") from None
    _require_candle_range(run_spec, candles)

    try:
        strategy_type = STRATEGY_REGISTRY[run_spec.request.strategy_key]
        strategy = strategy_type(**dict(run_spec.request.strategy_parameters))
    except (KeyError, TypeError, ValueError):
        raise WorkerError("strategy admission failed") from None
    result = BacktestEngine().run(
        strategy,
        candles,
        BacktestConfig(
            initial_capital=run_spec.request.initial_capital,
            commission_pct=run_spec.request.commission_pct,
            slippage_pct=run_spec.request.slippage_pct,
        ),
        symbol=run_spec.request.symbol,
    )
    result.run_id = run_spec.run_id
    metrics = compute_all_metrics(result)
    summary = {
        "schema_version": run_spec.schema_version,
        **result_summary(result, metrics),
        "image_digest": run_spec.image_digest,
        "dataset_sha256": run_spec.dataset.sha256,
        "completed_at": _utc_text(_parse_utc_datetime(clock())),
    }
    result_body = canonical_json_bytes(summary)
    trades_body = _parquet_bytes(trade_frame(result))
    equity_body = _parquet_bytes(equity_frame(result))
    report_body = generate_html_report(
        result,
        dict(metrics),
        chart_div_id=f"cloud-run-{run_spec.run_id}",
    ).encode("utf-8")

    outputs = (
        ("result.json", result_body, "application/json"),
        ("trades.parquet", trades_body, "application/vnd.apache.parquet"),
        ("equity-curve.parquet", equity_body, "application/vnd.apache.parquet"),
        ("report.html", report_body, "text/html; charset=utf-8"),
    )
    artifact_digests = [_artifact_digest("run-spec.json", run_spec_body)]
    artifact_lifecycle = (
        LifecycleClass.SELECTED_PUBLIC
        if run_spec.request.visibility is Visibility.PUBLIC
        else LifecycleClass.TRANSIENT
    )
    for name, body, content_type in outputs:
        object_store.put(
            f"{run_spec.result_prefix}{name}",
            body,
            content_type,
            lifecycle_class=artifact_lifecycle,
        )
        artifact_digests.append(_artifact_digest(name, body))
    manifest = ChecksumsManifest(artifacts=tuple(artifact_digests))
    object_store.put(
        f"{run_spec.result_prefix}checksums.json",
        canonical_json_bytes(manifest),
        "application/json",
        lifecycle_class=artifact_lifecycle,
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloud-backtest-worker")
    parser.add_argument("--run-spec-key", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Construct AWS adapters at runtime and return a shell-friendly status."""
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        import os

        bucket = os.environ["ARTIFACT_BUCKET"]
        table_name = os.environ["RUN_TABLE"]
        _validate_bucket(bucket)
        if not table_name:
            raise ValueError("RUN_TABLE must not be empty")
        import boto3

        object_store = S3ObjectStore(client=boto3.client("s3"), bucket=bucket)
        run_repository = DynamoRunRepository(table=boto3.resource("dynamodb").Table(table_name))
        execute_run(
            args.run_spec_key,
            object_store=object_store,
            run_repository=run_repository,
            clock=lambda: datetime.now(UTC),
        )
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "cloud.run.worker.failed",
            error_type=type(error).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
