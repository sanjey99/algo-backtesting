"""Internal canonical validation and lineage-rebase helpers for the store."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

import pandas as pd

from src.data.contracts import (
    AcquisitionRequest,
    ActionCoverage,
    CachePublicationError,
    ContractViolationError,
    LineageSegment,
    Provider,
)
from src.data.normalization import CANONICAL_COLUMNS, NUMERIC_COLUMNS
from src.data.quality import action_signature

__all__ = ("_merge_lineage", "_rebase_lineage", "_validate_canonical")


def _validate_canonical(frame: pd.DataFrame, request: AcquisitionRequest) -> None:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ContractViolationError("canonical cache frame must be non-empty")
    if tuple(frame.columns) != CANONICAL_COLUMNS:
        raise ContractViolationError("canonical cache columns are incompatible")
    expected_dtypes = [
        "datetime64[ns]",
        "string",
        *("float64" for _ in NUMERIC_COLUMNS),
        "string",
    ]
    if frame.dtypes.astype(str).tolist() != expected_dtypes:
        raise ContractViolationError("canonical cache dtypes are incompatible")
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    if (
        timestamps.tz is not None
        or not timestamps.is_monotonic_increasing
        or timestamps.has_duplicates
    ):
        raise ContractViolationError("canonical cache timestamps are incompatible")
    if set(frame["symbol"].astype(str)) != {request.symbol}:
        raise ContractViolationError("canonical cache symbol is incompatible")
    if not set(frame["source"].astype(str)) <= {provider.value for provider in Provider}:
        raise ContractViolationError("canonical cache source is incompatible")
    for _, row in frame.iterrows():
        values = {column: float(row[column]) for column in NUMERIC_COLUMNS}
        if not all(math.isfinite(value) for value in values.values()):
            raise ContractViolationError("canonical cache contains non-finite values")
        if any(values[column] <= 0 for column in ("open", "high", "low", "close", "adj_close")):
            raise ContractViolationError("canonical cache contains non-positive prices")
        if values["volume"] < 0 or values["dividend_amount"] < 0:
            raise ContractViolationError("canonical cache contains negative quantities")
        if values["split_coefficient"] <= 0:
            raise ContractViolationError("canonical cache contains an invalid split coefficient")
        if values["high"] < max(values["open"], values["close"]):
            raise ContractViolationError("canonical cache high is inconsistent")
        if values["low"] > min(values["open"], values["close"]):
            raise ContractViolationError("canonical cache low is inconsistent")


def _merge_lineage(
    existing_manifest: Mapping[str, Any],
    incoming: tuple[LineageSegment, ...],
) -> tuple[LineageSegment, ...]:
    existing = _lineage_from_document(existing_manifest.get("lineage", []))
    unique = {
        (
            segment.start,
            segment.end,
            segment.provider,
            segment.acquired_at,
            segment.action_coverage,
            segment.content_hash,
            segment.action_signature,
        ): segment
        for segment in (*existing, *incoming)
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (
                item[0],
                item[1],
                item[2].value,
                item[3],
                item[5],
                item[6],
            ),
        )
    )


def _rebase_lineage(
    existing_manifest: Mapping[str, Any],
    incoming: tuple[LineageSegment, ...],
    assembled: pd.DataFrame,
    replace_ranges: tuple[tuple[date, date], ...],
) -> tuple[LineageSegment, ...]:
    existing = _lineage_from_document(existing_manifest.get("lineage", []))
    provenance: list[tuple[Provider, datetime, ActionCoverage, date, date, str]] = []
    for timestamp_value in assembled["timestamp"]:
        timestamp = pd.Timestamp(timestamp_value).date()
        refreshed = any(start <= timestamp <= end for start, end in replace_ranges)
        candidates = incoming if refreshed else existing
        segment = next(
            (item for item in candidates if item.start <= timestamp <= item.end),
            None,
        )
        if segment is None:
            raise CachePublicationError("rebased cache lineage does not cover every row")
        provenance.append(
            (
                segment.provider,
                segment.acquired_at,
                segment.action_coverage,
                segment.start,
                segment.end,
                segment.content_hash,
            )
        )

    result: list[LineageSegment] = []
    first = 0
    for index in range(1, len(assembled) + 1):
        if index < len(assembled) and provenance[index] == provenance[first]:
            continue
        frame = assembled.iloc[first:index].copy(deep=True).reset_index(drop=True)
        provider, acquired_at, action_coverage, _, _, _ = provenance[first]
        result.append(
            LineageSegment(
                pd.Timestamp(frame.iloc[0]["timestamp"]).date(),
                pd.Timestamp(frame.iloc[-1]["timestamp"]).date(),
                provider,
                acquired_at,
                action_coverage,
                hashlib.sha256(
                    frame.to_json(
                        orient="records",
                        date_format="iso",
                        double_precision=15,
                    ).encode()
                ).hexdigest(),
                action_signature(frame),
            )
        )
        first = index
    return tuple(result)


def _lineage_from_document(value: object) -> tuple[LineageSegment, ...]:
    if not isinstance(value, list):
        raise CachePublicationError("embedded acquisition manifest lineage is incompatible")
    segments: list[LineageSegment] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "start",
            "end",
            "provider",
            "acquired_at",
            "action_coverage",
            "content_hash",
            "action_signature",
        }:
            raise CachePublicationError("embedded acquisition manifest lineage is incompatible")
        try:
            start = date.fromisoformat(cast(str, item["start"]))
            end = date.fromisoformat(cast(str, item["end"]))
            provider = Provider(item["provider"])
            acquired_at = _parse_utc_timestamp(item["acquired_at"])
            action_coverage = ActionCoverage(item["action_coverage"])
            content_hash = item["content_hash"]
            action_signature_value = item["action_signature"]
        except (TypeError, ValueError) as error:
            raise CachePublicationError(
                "embedded acquisition manifest lineage is incompatible"
            ) from error
        if (
            start > end
            or not _is_sha256(content_hash)
            or not isinstance(action_signature_value, str)
            or not action_signature_value
        ):
            raise CachePublicationError("embedded acquisition manifest lineage is incompatible")
        segments.append(
            LineageSegment(
                start,
                end,
                provider,
                acquired_at,
                action_coverage,
                content_hash,
                action_signature_value,
            )
        )
    return tuple(segments)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp evidence must be an ISO string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("timestamp evidence must be UTC")
    return parsed
