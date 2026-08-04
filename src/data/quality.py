"""Pure canonical market-data quality evaluation and action evidence."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, cast

import pandas as pd

from src.data.calendars import group_contiguous_sessions
from src.data.contracts import (
    AcquisitionRequest,
    Provider,
    QualityFinding,
    QualityPolicy,
    QualitySeverity,
    RejectedRow,
)
from src.data.normalization import (
    CANONICAL_COLUMNS,
    NUMERIC_COLUMNS,
    NormalizationResult,
)

_VALUE_COLUMNS = CANONICAL_COLUMNS[1:]
_REVISION_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_close",
    "dividend_amount",
    "split_coefficient",
)
_SEVERITY_RANK = {
    QualitySeverity.INFO: 0,
    QualitySeverity.WARNING: 1,
    QualitySeverity.FATAL: 2,
}


@dataclass(frozen=True, slots=True)
class ReconciliationCounters:
    provider_rows: int = 0
    timestamp_unclassifiable_rows: int = 0
    out_of_range_rows: int = 0
    in_range_rows: int = 0
    dedupe_input_rows: int = 0
    exact_duplicate_rows_removed: int = 0
    conflicting_duplicate_rows: int = 0
    nonconflicting_unique_rows: int = 0
    accepted_unique_rows: int = 0
    rejected_unique_rows: int = 0
    expected_sessions: int = 0
    accepted_expected_sessions: int = 0
    missing_sessions: int = 0

    def assert_valid(self) -> None:
        assert self.provider_rows == (
            self.timestamp_unclassifiable_rows + self.out_of_range_rows + self.in_range_rows
        )
        assert self.in_range_rows == self.dedupe_input_rows
        assert self.dedupe_input_rows == (
            self.exact_duplicate_rows_removed
            + self.conflicting_duplicate_rows
            + self.nonconflicting_unique_rows
        )
        assert self.nonconflicting_unique_rows == (
            self.accepted_unique_rows + self.rejected_unique_rows
        )
        assert self.expected_sessions == (
            self.accepted_expected_sessions + self.missing_sessions
        )


@dataclass(frozen=True, slots=True)
class QualityResult:
    severity: QualitySeverity
    findings: tuple[QualityFinding, ...]
    rejected_rows: tuple[RejectedRow, ...]
    counters: ReconciliationCounters
    coverage: float | None = None
    max_consecutive_missing_sessions: int | None = None
    action_signature: str | None = None
    _frame: pd.DataFrame | None = field(default=None, repr=False, compare=False)

    @property
    def frame(self) -> pd.DataFrame | None:
        return None if self._frame is None else self._frame.copy(deep=True)

    @property
    def accepted_frame(self) -> pd.DataFrame | None:
        return self.frame

    @property
    def is_fatal(self) -> bool:
        return self.severity is QualitySeverity.FATAL


@dataclass(frozen=True, slots=True)
class CachedRevision:
    timestamp: pd.Timestamp
    field: str
    cached_value: float | None
    refreshed_value: float | None


def _severity(findings: tuple[QualityFinding, ...]) -> QualitySeverity:
    return max(
        (finding.severity for finding in findings),
        key=_SEVERITY_RANK.__getitem__,
        default=QualitySeverity.INFO,
    )


def _same_duplicate_value(left: object, right: object) -> bool:
    try:
        if pd.isna(cast(Any, left)) and pd.isna(cast(Any, right)):
            return True
    except (TypeError, ValueError):
        pass
    return bool(left == right)


def _rows_identical(group: pd.DataFrame) -> bool:
    first = group.iloc[0]
    return all(
        _same_duplicate_value(first[column], row[column])
        for _, row in group.iloc[1:].iterrows()
        for column in _VALUE_COLUMNS
    )


def _row_reasons(row: pd.Series) -> tuple[str, ...]:
    reasons: list[str] = []
    values: dict[str, float] = {}
    for column in NUMERIC_COLUMNS:
        value = row[column]
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            reasons.append(f"{column}_not_finite_numeric")
        else:
            values[column] = float(value)
    for column in ("open", "high", "low", "close", "adj_close"):
        if column in values and values[column] <= 0:
            reasons.append(f"{column}_not_positive")
    if "volume" in values and values["volume"] < 0:
        reasons.append("volume_negative")
    if "dividend_amount" in values and values["dividend_amount"] < 0:
        reasons.append("dividend_amount_negative")
    if "split_coefficient" in values and values["split_coefficient"] <= 0:
        reasons.append("split_coefficient_not_positive")
    if {"high", "open", "close"} <= values.keys() and values["high"] < max(
        values["open"], values["close"]
    ):
        reasons.append("high_below_open_or_close")
    if {"low", "open", "close"} <= values.keys() and values["low"] > min(
        values["open"], values["close"]
    ):
        reasons.append("low_above_open_or_close")
    if {"adj_close", "close"} <= values.keys() and values["close"] != 0:
        factor = values["adj_close"] / values["close"]
        if not math.isfinite(factor) or factor <= 0:
            reasons.append("adjustment_factor_not_positive_finite")
    return tuple(reasons)


def evaluate_range_candidate(
    normalized: NormalizationResult,
    expected_sessions: pd.DatetimeIndex,
    policy: QualityPolicy | None = None,
) -> QualityResult:
    """Apply structural rules to one provider range; defer coverage severity."""
    del policy
    expected = pd.DatetimeIndex(expected_sessions).tz_localize(None).normalize()
    findings = normalized.findings
    frame = normalized.candidate_frame
    exact_removed = 0
    conflicting_rows = 0
    retained: list[pd.Series] = []

    for _, group in frame.groupby("timestamp", sort=False, dropna=False):
        if len(group) == 1:
            retained.append(group.iloc[0])
        elif _rows_identical(group):
            exact_removed += len(group) - 1
            retained.append(group.iloc[0])
        else:
            conflicting_rows += len(group)

    if exact_removed:
        findings += (
            QualityFinding(
                QualitySeverity.WARNING,
                "exact_duplicates_removed",
                "identical duplicate rows were removed",
                {"rows": exact_removed},
            ),
        )
    if conflicting_rows:
        findings += (
            QualityFinding(
                QualitySeverity.FATAL,
                "conflicting_duplicates",
                "conflicting timestamp duplicates make the candidate unusable",
                {"rows": conflicting_rows},
            ),
        )

    accepted: list[dict[str, Any]] = []
    rejected = list(normalized.timestamp_rejections)
    calendar_mismatches = 0
    for row in retained:
        reasons = _row_reasons(row)
        timestamp = pd.Timestamp(row["timestamp"])
        if not reasons and timestamp not in expected:
            reasons = ("calendar_mismatch",)
            calendar_mismatches += 1
        if reasons:
            rejected.append(
                RejectedRow(
                    int(row["_source_row_number"]),
                    ",".join(reasons),
                    timestamp.date().isoformat(),
                )
            )
        else:
            accepted.append({column: row[column] for column in CANONICAL_COLUMNS})

    rejected_unique = len(retained) - len(accepted)
    if rejected_unique:
        findings += (
            QualityFinding(
                QualitySeverity.WARNING,
                "rejected_rows",
                "isolated invalid unique rows were rejected",
                {"rows": rejected_unique},
            ),
        )
    if calendar_mismatches:
        findings += (
            QualityFinding(
                QualitySeverity.FATAL,
                "calendar_mismatch",
                "valid provider rows do not belong to the expected exchange calendar",
                {"rows": calendar_mismatches},
            ),
        )
    if not accepted:
        findings += (
            QualityFinding(
                QualitySeverity.FATAL,
                "no_accepted_rows",
                "candidate has no structurally accepted rows",
            ),
        )

    accepted_frame = _canonical_frame(accepted)
    accepted_expected = len(set(accepted_frame["timestamp"]) & set(expected))
    counters = ReconciliationCounters(
        provider_rows=normalized.counters.provider_rows,
        timestamp_unclassifiable_rows=normalized.counters.timestamp_unclassifiable_rows,
        out_of_range_rows=normalized.counters.out_of_range_rows,
        in_range_rows=normalized.counters.in_range_rows,
        dedupe_input_rows=normalized.counters.dedupe_input_rows,
        exact_duplicate_rows_removed=exact_removed,
        conflicting_duplicate_rows=conflicting_rows,
        nonconflicting_unique_rows=len(retained),
        accepted_unique_rows=len(accepted),
        rejected_unique_rows=rejected_unique,
        expected_sessions=len(expected),
        accepted_expected_sessions=accepted_expected,
        missing_sessions=len(expected) - accepted_expected,
    )
    counters.assert_valid()
    severity = _severity(findings)
    return QualityResult(
        severity=severity,
        findings=findings,
        rejected_rows=tuple(rejected),
        counters=counters,
        action_signature=None if accepted_frame.empty else action_signature(accepted_frame),
        _frame=None if severity is QualitySeverity.FATAL else accepted_frame,
    )


def evaluate_complete_request(
    frame: pd.DataFrame,
    request: AcquisitionRequest,
    expected_sessions: pd.DatetimeIndex,
    policy: QualityPolicy | None = None,
) -> QualityResult:
    """Validate a complete merged request and apply final coverage/gap policy once."""
    applied_policy = policy or QualityPolicy()
    candidate = frame.copy(deep=True)
    expected = pd.DatetimeIndex(expected_sessions).tz_localize(None).normalize()
    findings: tuple[QualityFinding, ...] = ()
    contract_errors = _canonical_contract_errors(candidate, request)
    accepted_expected = 0
    missing = expected
    if not contract_errors:
        accepted = pd.DatetimeIndex(candidate["timestamp"])
        unexpected = accepted.difference(expected)
        if len(unexpected):
            contract_errors.append("calendar_mismatch")
        accepted_expected = len(accepted.intersection(expected))
        missing = expected.difference(accepted)
    if contract_errors:
        findings += (
            QualityFinding(
                QualitySeverity.FATAL,
                "canonical_contract_failed",
                "complete merged frame failed the canonical contract",
                {"reasons": tuple(contract_errors)},
            ),
        )
    if "calendar_mismatch" in contract_errors:
        findings += (
            QualityFinding(
                QualitySeverity.FATAL,
                "calendar_mismatch",
                "complete frame contains non-session timestamps",
            ),
        )

    expected_count = len(expected)
    coverage = accepted_expected / expected_count if expected_count else 1.0
    groups = group_contiguous_sessions(expected, missing)
    expected_positions = {timestamp: index for index, timestamp in enumerate(expected)}
    max_gap = max(
        (
            expected_positions[end] - expected_positions[start] + 1
            for start, end in groups
        ),
        default=0,
    )
    if not contract_errors:
        if coverage < applied_policy.minimum_coverage:
            findings += (
                QualityFinding(
                    QualitySeverity.FATAL,
                    "insufficient_coverage",
                    "complete request coverage is below policy",
                    {"coverage": coverage, "minimum": applied_policy.minimum_coverage},
                ),
            )
        if max_gap > applied_policy.max_consecutive_missing_sessions:
            findings += (
                QualityFinding(
                    QualitySeverity.FATAL,
                    "excessive_gap",
                    "complete request has too many consecutive missing sessions",
                    {
                        "maximum_gap": max_gap,
                        "allowed": applied_policy.max_consecutive_missing_sessions,
                    },
                ),
            )
        if len(missing) and all(
            finding.severity is not QualitySeverity.FATAL for finding in findings
        ):
            findings += (
                QualityFinding(
                    QualitySeverity.WARNING,
                    "missing_sessions",
                    "complete request has limited missing sessions",
                    {"sessions": len(missing), "maximum_gap": max_gap},
                ),
            )

    counters = ReconciliationCounters(
        provider_rows=len(candidate),
        in_range_rows=len(candidate),
        dedupe_input_rows=len(candidate),
        nonconflicting_unique_rows=len(candidate),
        accepted_unique_rows=len(candidate),
        expected_sessions=expected_count,
        accepted_expected_sessions=accepted_expected,
        missing_sessions=expected_count - accepted_expected,
    )
    counters.assert_valid()
    severity = _severity(findings)
    canonical = None if severity is QualitySeverity.FATAL else candidate
    return QualityResult(
        severity=severity,
        findings=findings,
        rejected_rows=(),
        counters=counters,
        coverage=coverage,
        max_consecutive_missing_sessions=max_gap,
        action_signature=None if canonical is None else action_signature(canonical),
        _frame=canonical,
    )


def _canonical_contract_errors(frame: pd.DataFrame, request: AcquisitionRequest) -> list[str]:
    errors: list[str] = []
    if tuple(frame.columns) != CANONICAL_COLUMNS:
        return ["columns"]
    expected_dtypes = ["datetime64[ns]", "string", *(["float64"] * 8), "string"]
    if frame.dtypes.astype(str).tolist() != expected_dtypes:
        errors.append("dtypes")
    if frame.empty:
        errors.append("no_accepted_rows")
        return errors
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    if timestamps.tz is not None or not timestamps.is_monotonic_increasing:
        errors.append("timestamp_order")
    if timestamps.has_duplicates:
        errors.append("timestamp_duplicates")
    if set(frame["symbol"].astype(str)) != {request.symbol}:
        errors.append("symbol")
    if not set(frame["source"].astype(str)) <= {item.value for item in Provider}:
        errors.append("source")
    if any(_row_reasons(row) for _, row in frame.iterrows()):
        errors.append("row_values")
    return errors


def _canonical_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            {
                "timestamp": pd.Series(dtype="datetime64[ns]"),
                "symbol": pd.Series(dtype="string"),
                **{column: pd.Series(dtype="float64") for column in NUMERIC_COLUMNS},
                "source": pd.Series(dtype="string"),
            }
        ).loc[:, list(CANONICAL_COLUMNS)]
    frame = pd.DataFrame.from_records(rows).loc[:, list(CANONICAL_COLUMNS)]
    frame["timestamp"] = frame["timestamp"].astype("datetime64[ns]")
    frame["symbol"] = frame["symbol"].astype("string")
    for column in NUMERIC_COLUMNS:
        frame[column] = frame[column].astype("float64")
    frame["source"] = frame["source"].astype("string")
    return frame.reset_index(drop=True)


def action_signature(frame: pd.DataFrame) -> str:
    """Hash canonical sorted corporate-action tuples deterministically."""
    tuples: list[list[str | float]] = []
    for _, row in frame.sort_values("timestamp", kind="mergesort").iterrows():
        dividend = float(row["dividend_amount"])
        split = float(row["split_coefficient"])
        if not math.isfinite(dividend) or dividend < 0:
            raise ValueError("dividend_amount must be finite and nonnegative")
        if not math.isfinite(split) or split <= 0:
            raise ValueError("split_coefficient must be finite and positive")
        tuples.append(
            [pd.Timestamp(row["timestamp"]).date().isoformat(), dividend, split]
        )
    payload = json.dumps(tuples, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def compare_cached_and_refreshed(
    cached: pd.DataFrame,
    refreshed: pd.DataFrame,
) -> tuple[CachedRevision, ...]:
    """Report every material cached-versus-refreshed numeric value change."""
    cached_rows = _indexed_unique_rows(cached)
    refreshed_rows = _indexed_unique_rows(refreshed)
    revisions: list[CachedRevision] = []
    for timestamp in sorted(set(cached_rows) | set(refreshed_rows)):
        old = cached_rows.get(timestamp)
        new = refreshed_rows.get(timestamp)
        for column in _REVISION_COLUMNS:
            old_value = None if old is None else float(old[column])
            new_value = None if new is None else float(new[column])
            equal = (
                old_value is not None
                and new_value is not None
                and math.isclose(old_value, new_value, rel_tol=1e-9, abs_tol=1e-12)
            )
            if not equal:
                revisions.append(CachedRevision(timestamp, column, old_value, new_value))
    return tuple(revisions)


def _indexed_unique_rows(frame: pd.DataFrame) -> dict[pd.Timestamp, pd.Series]:
    if "timestamp" not in frame or frame["timestamp"].duplicated().any():
        raise ValueError("refresh comparison requires unique timestamps")
    return {pd.Timestamp(row["timestamp"]): row for _, row in frame.iterrows()}


evaluate_quality = evaluate_range_candidate
compare_refresh = compare_cached_and_refreshed
