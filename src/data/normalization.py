"""Pure provider-native to canonical-candidate normalization."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast
from zoneinfo import ZoneInfoNotFoundError

import pandas as pd

from src.data.contracts import (
    ActionCoverage,
    Provider,
    ProviderBatch,
    QualityFinding,
    QualitySeverity,
    RejectedRow,
)

CANONICAL_COLUMNS = (
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_close",
    "dividend_amount",
    "split_coefficient",
    "source",
)
NUMERIC_COLUMNS = CANONICAL_COLUMNS[2:-1]
_INTERNAL_COLUMNS = (*CANONICAL_COLUMNS, "_source_row_number")
_YFINANCE_FIELDS = MappingProxyType(
    {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "adj close": "adj_close",
        "dividends": "dividend_amount",
        "stock splits": "split_coefficient",
    }
)
_ALPHA_VANTAGE_FIELDS = MappingProxyType(
    {
        "1. open": "open",
        "2. high": "high",
        "3. low": "low",
        "4. close": "close",
        "6. volume": "volume",
        "5. adjusted close": "adj_close",
        "7. dividend amount": "dividend_amount",
        "8. split coefficient": "split_coefficient",
    }
)
_ESSENTIAL_FIELDS = frozenset({"open", "high", "low", "close", "volume", "adj_close"})


@dataclass(frozen=True, slots=True)
class NormalizationCounters:
    """Disjoint timestamp/range classification counters."""

    provider_rows: int
    timestamp_unclassifiable_rows: int
    out_of_range_rows: int
    in_range_rows: int
    dedupe_input_rows: int

    def assert_valid(self) -> None:
        assert self.provider_rows == (
            self.timestamp_unclassifiable_rows + self.out_of_range_rows + self.in_range_rows
        )
        assert self.in_range_rows == self.dedupe_input_rows


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Immutable evidence plus an internal, defensively exposed candidate frame."""

    provider: Provider
    counters: NormalizationCounters
    findings: tuple[QualityFinding, ...] = ()
    timestamp_rejections: tuple[RejectedRow, ...] = ()
    _candidate_frame: pd.DataFrame = field(
        default_factory=pd.DataFrame,
        repr=False,
        compare=False,
    )

    @property
    def candidate_frame(self) -> pd.DataFrame:
        return self._candidate_frame.loc[:, list(CANONICAL_COLUMNS)].copy(deep=True)

    @property
    def is_fatal(self) -> bool:
        return any(finding.severity is QualitySeverity.FATAL for finding in self.findings)


def _empty_candidate_frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "timestamp": pd.Series(dtype="datetime64[ns]"),
            "symbol": pd.Series(dtype="string"),
            **{column: pd.Series(dtype="float64") for column in NUMERIC_COLUMNS},
            "source": pd.Series(dtype="string"),
            "_source_row_number": pd.Series(dtype="int64"),
        }
    )
    return frame.loc[:, list(_INTERNAL_COLUMNS)]


def _provider_columns(batch: ProviderBatch) -> tuple[dict[str, object], dict[str, str]]:
    frame = batch.frame
    fields = (
        _YFINANCE_FIELDS if batch.provider is Provider.YFINANCE else _ALPHA_VANTAGE_FIELDS
    )
    native: dict[str, object] = {}
    seen_symbols: set[str] = set()
    for column in frame.columns:
        if isinstance(column, tuple):
            name = str(column[0]).strip().lower()
            if len(column) > 1 and str(column[1]).strip():
                seen_symbols.add(str(column[1]).strip().upper())
        else:
            name = str(column).strip().lower()
        if name in native:
            raise ValueError(f"ambiguous provider field {name!r}")
        native[name] = column
    if seen_symbols and seen_symbols != {batch.request.symbol}:
        raise ValueError("provider frame contains the wrong symbol")
    return native, dict(fields)


def _parse_daily_timestamp(value: object, native_timezone: str | None) -> pd.Timestamp | None:
    if value is None or isinstance(value, bool | int | float):
        return None
    try:
        parsed = pd.Timestamp(cast(Any, value))
        if pd.isna(parsed):
            return None
        if parsed.tzinfo is None and native_timezone is not None:
            parsed = parsed.tz_localize(native_timezone, ambiguous="raise", nonexistent="raise")
        return parsed.normalize().tz_localize(None)
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return None


def _as_float(value: object) -> float:
    if value is None or isinstance(value, bool):
        return float("nan")
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return float("nan")


def normalize_provider_batch(batch: ProviderBatch) -> NormalizationResult:
    """Map one provider batch without I/O, mutation, deduplication, or row validation."""
    provider_rows = len(batch.frame)
    try:
        native_columns, field_mapping = _provider_columns(batch)
    except ValueError as error:
        return _fatal_schema_result(batch, provider_rows, str(error))

    available = {
        canonical for native, canonical in field_mapping.items() if native in native_columns
    }
    missing_essential = sorted(_ESSENTIAL_FIELDS - available)
    if missing_essential:
        return _fatal_schema_result(
            batch,
            provider_rows,
            f"missing required provider fields: {', '.join(missing_essential)}",
        )

    start = pd.Timestamp(batch.request.start)
    end = pd.Timestamp(batch.request.end)
    timestamp_rejections: list[RejectedRow] = []
    rows: list[dict[str, Any]] = []
    unclassifiable = 0
    out_of_range = 0

    for source_row_number, (timestamp_value, provider_row) in enumerate(batch.frame.iterrows()):
        timestamp = _parse_daily_timestamp(timestamp_value, batch.native_timezone)
        if timestamp is None:
            unclassifiable += 1
            timestamp_rejections.append(
                RejectedRow(source_row_number, "timestamp_unclassifiable")
            )
            continue
        if timestamp < start or timestamp > end:
            out_of_range += 1
            continue
        canonical: dict[str, Any] = {
            "timestamp": timestamp,
            "symbol": batch.request.symbol,
            "source": batch.provider.value,
            "_source_row_number": source_row_number,
        }
        for native_name, canonical_name in field_mapping.items():
            if native_name in native_columns:
                native_column = cast(Any, native_columns[native_name])
                canonical[canonical_name] = _as_float(provider_row.loc[native_column])
        if batch.action_coverage is ActionCoverage.REPRESENTED:
            canonical.setdefault("dividend_amount", 0.0)
            canonical.setdefault("split_coefficient", 1.0)
        else:
            canonical.setdefault("dividend_amount", float("nan"))
            canonical.setdefault("split_coefficient", float("nan"))
        rows.append(canonical)

    candidate = _candidate_frame(rows)
    candidate = candidate.sort_values(
        ["timestamp", "_source_row_number"], kind="mergesort", ignore_index=True
    )
    in_range = len(candidate)
    counters = NormalizationCounters(
        provider_rows=provider_rows,
        timestamp_unclassifiable_rows=unclassifiable,
        out_of_range_rows=out_of_range,
        in_range_rows=in_range,
        dedupe_input_rows=in_range,
    )
    counters.assert_valid()
    findings: tuple[QualityFinding, ...] = (
        QualityFinding(
            QualitySeverity.INFO,
            "normalized",
            "provider rows mapped, range-filtered, and stably sorted",
            {"provider": batch.provider.value},
        ),
    )
    if timestamp_rejections:
        findings += (
            QualityFinding(
                QualitySeverity.WARNING,
                "timestamp_unclassifiable",
                "provider rows had unclassifiable timestamps",
                {"rows": len(timestamp_rejections)},
            ),
        )
    return NormalizationResult(
        provider=batch.provider,
        counters=counters,
        findings=findings,
        timestamp_rejections=tuple(timestamp_rejections),
        _candidate_frame=candidate,
    )


def _candidate_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return _empty_candidate_frame()
    frame = pd.DataFrame.from_records(rows)
    frame["timestamp"] = pd.Series(frame["timestamp"], dtype="datetime64[ns]")
    frame["symbol"] = frame["symbol"].astype("string")
    for column in NUMERIC_COLUMNS:
        frame[column] = frame[column].astype("float64")
    frame["source"] = frame["source"].astype("string")
    frame["_source_row_number"] = frame["_source_row_number"].astype("int64")
    return frame.loc[:, list(_INTERNAL_COLUMNS)]


def _fatal_schema_result(
    batch: ProviderBatch,
    provider_rows: int,
    reason: str,
) -> NormalizationResult:
    counters = NormalizationCounters(provider_rows, provider_rows, 0, 0, 0)
    counters.assert_valid()
    return NormalizationResult(
        provider=batch.provider,
        counters=counters,
        findings=(
            QualityFinding(
                QualitySeverity.FATAL,
                "unusable_schema",
                "provider frame cannot be mapped to the canonical schema",
                {"reason": reason},
            ),
        ),
        _candidate_frame=_empty_candidate_frame(),
    )


def _diagnostic_candidate_frame(result: NormalizationResult) -> pd.DataFrame:
    """Return internal source-row evidence for quality duplicate diagnostics only."""
    return result._candidate_frame.copy(deep=True)


normalize_batch = normalize_provider_batch
