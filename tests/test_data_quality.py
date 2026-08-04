"""Hand-derived quality, reconciliation, action, and refresh cases."""
from __future__ import annotations

import hashlib
import json
from datetime import date

import pandas as pd
import pytest

from src.data.contracts import (
    AcquisitionRequest,
    ActionCoverage,
    Provider,
    ProviderBatch,
    QualityPolicy,
    QualitySeverity,
)
from src.data.normalization import CANONICAL_COLUMNS, normalize_provider_batch
from src.data.quality import (
    action_signature,
    compare_cached_and_refreshed,
    evaluate_complete_request,
    evaluate_range_candidate,
)


def _request(start: str = "2024-01-02", end: str = "2024-01-05") -> AcquisitionRequest:
    return AcquisitionRequest("AAPL", date.fromisoformat(start), date.fromisoformat(end))


def _raw(rows: list[dict[str, object]], timestamps: list[object]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=timestamps)


def _row(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Open": 10.0,
        "High": 11.0,
        "Low": 9.0,
        "Close": 10.5,
        "Adj Close": 10.25,
        "Volume": 100.0,
        "Dividends": 0.0,
        "Stock Splits": 1.0,
    }
    return {**base, **changes}


def _normalized(
    rows: list[dict[str, object]],
    timestamps: list[object],
    *,
    request: AcquisitionRequest | None = None,
    action_coverage: ActionCoverage = ActionCoverage.REPRESENTED,
):
    frame = _raw(rows, timestamps)
    batch = ProviderBatch(
        Provider.YFINANCE,
        request or _request(),
        frame,
        raw_row_count=len(frame),
        action_coverage=action_coverage,
    )
    return normalize_provider_batch(batch)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"Open": None}, "open_not_finite_numeric"),
        ({"Open": "bad"}, "open_not_finite_numeric"),
        ({"Open": float("nan")}, "open_not_finite_numeric"),
        ({"Open": float("inf")}, "open_not_finite_numeric"),
        ({"High": 10.4}, "high_below_open_or_close"),
        ({"Low": 10.1}, "low_above_open_or_close"),
        ({"Close": 0.0}, "close_not_positive"),
        ({"Volume": -1.0}, "volume_negative"),
        ({"Adj Close": float("inf")}, "adj_close_not_finite_numeric"),
        ({"Dividends": -0.1}, "dividend_amount_negative"),
        ({"Stock Splits": 0.0}, "split_coefficient_not_positive"),
    ],
)
def test_rejects_each_invalid_numeric_and_ohlcv_case(
    change: dict[str, object], reason: str
) -> None:
    normalized = _normalized([_row(), _row(**change)], ["2024-01-02", "2024-01-03"])

    result = evaluate_range_candidate(
        normalized, pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    )

    assert result.severity is QualitySeverity.WARNING
    assert result.frame is not None
    assert len(result.frame) == 1
    assert reason in result.rejected_rows[0].reason.split(",")


def test_missing_actions_reject_rows_when_provider_did_not_declare_representation() -> None:
    row = _row()
    del row["Dividends"]
    del row["Stock Splits"]
    normalized = _normalized(
        [row, _row()],
        ["2024-01-02", "2024-01-03"],
        action_coverage=ActionCoverage.UNKNOWN,
    )

    result = evaluate_range_candidate(
        normalized, pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    )

    assert result.frame is not None and len(result.frame) == 1
    assert "dividend_amount_not_finite_numeric" in result.rejected_rows[0].reason


def test_identical_duplicates_keep_first_and_reconcile_after_unique_row_validation() -> None:
    invalid_duplicate = _row(High=9.0)
    normalized = _normalized(
        [invalid_duplicate, invalid_duplicate, _row(Low=11.0), _row()],
        ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04"],
    )

    result = evaluate_range_candidate(
        normalized,
        pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    counters = result.counters

    assert result.frame is not None
    assert result.frame["timestamp"].tolist() == [pd.Timestamp("2024-01-04")]
    assert counters.provider_rows == 4
    assert counters.in_range_rows == counters.dedupe_input_rows == 4
    assert counters.exact_duplicate_rows_removed == 1
    assert counters.conflicting_duplicate_rows == 0
    assert counters.nonconflicting_unique_rows == 3
    assert counters.accepted_unique_rows == 1
    assert counters.rejected_unique_rows == 2
    counters.assert_valid()


def test_conflicting_duplicate_group_counts_every_member_and_is_fatal() -> None:
    normalized = _normalized(
        [_row(Open=10.0), _row(Open=10.1), _row()],
        ["2024-01-02", "2024-01-02", "2024-01-03"],
    )

    result = evaluate_range_candidate(
        normalized, pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    )

    assert result.is_fatal is True
    assert result.frame is None
    assert result.counters.conflicting_duplicate_rows == 2
    assert result.counters.nonconflicting_unique_rows == 1
    assert any(finding.code == "conflicting_duplicates" for finding in result.findings)
    result.counters.assert_valid()


def test_valid_weekend_or_holiday_row_is_a_fatal_calendar_mismatch() -> None:
    normalized = _normalized(
        [_row(), _row()],
        ["2024-01-02", "2024-01-06"],
        request=_request("2024-01-02", "2024-01-06"),
    )

    result = evaluate_range_candidate(normalized, pd.DatetimeIndex(["2024-01-02"]))

    assert result.is_fatal is True
    assert result.frame is None
    assert any(finding.code == "calendar_mismatch" for finding in result.findings)


@pytest.mark.parametrize("accepted_count", [1, 2])
def test_partial_provider_ranges_apply_structural_checks_without_local_coverage_fatality(
    accepted_count: int,
) -> None:
    timestamps = [f"2024-01-0{day}" for day in range(2, 2 + accepted_count)]
    normalized = _normalized([_row() for _ in timestamps], timestamps)
    full_expected = pd.date_range("2024-01-02", periods=100, freq="D")

    result = evaluate_range_candidate(normalized, full_expected)

    assert result.is_fatal is False
    assert result.frame is not None and len(result.frame) == accepted_count
    assert not any(
        finding.code in {"insufficient_coverage", "excessive_gap"}
        for finding in result.findings
    )


def test_complete_merge_warns_and_reconciles_expected_sessions() -> None:
    frame = _canonical_frame(["2024-01-02", "2024-01-03", "2024-01-05"])
    expected = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])

    result = evaluate_complete_request(
        frame,
        _request(),
        expected,
        QualityPolicy(minimum_coverage=0.75, max_consecutive_missing_sessions=2),
    )

    assert result.severity is QualitySeverity.WARNING
    assert result.frame is not None
    assert result.coverage == 0.75
    assert result.max_consecutive_missing_sessions == 1
    assert result.counters.expected_sessions == 4
    assert result.counters.accepted_expected_sessions == 3
    assert result.counters.missing_sessions == 1
    result.counters.assert_valid()


@pytest.mark.parametrize(
    ("timestamps", "policy", "code"),
    [
        (
            ["2024-01-02", "2024-01-03"],
            QualityPolicy(minimum_coverage=0.98, max_consecutive_missing_sessions=2),
            "insufficient_coverage",
        ),
        (
            ["2024-01-02", "2024-01-05"],
            QualityPolicy(minimum_coverage=0.40, max_consecutive_missing_sessions=1),
            "excessive_gap",
        ),
    ],
)
def test_complete_merge_fails_closed_for_coverage_or_consecutive_gap(
    timestamps: list[str], policy: QualityPolicy, code: str
) -> None:
    expected = pd.DatetimeIndex(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    )

    result = evaluate_complete_request(_canonical_frame(timestamps), _request(), expected, policy)

    assert result.is_fatal is True
    assert result.frame is None
    assert any(finding.code == code for finding in result.findings)


@pytest.mark.parametrize(
    "change",
    [
        {"adj_close": 0.0},
        {"adj_close": float("inf")},
        {"close": float("nan")},
    ],
)
def test_complete_contract_rejects_invalid_adjustment_factors(change: dict[str, float]) -> None:
    frame = _canonical_frame(["2024-01-02"])
    for field, value in change.items():
        frame.loc[0, field] = value

    result = evaluate_complete_request(
        frame, _request("2024-01-02", "2024-01-02"), pd.DatetimeIndex(["2024-01-02"])
    )

    assert result.is_fatal is True
    assert result.frame is None


def test_action_signature_is_sha256_of_sorted_canonical_action_tuples() -> None:
    frame = _canonical_frame(["2024-01-03", "2024-01-02"])
    frame.loc[0, ["dividend_amount", "split_coefficient"]] = [0.5, 1.0]
    frame.loc[1, ["dividend_amount", "split_coefficient"]] = [0.0, 2.0]
    canonical_json = json.dumps(
        [["2024-01-02", 0.0, 2.0], ["2024-01-03", 0.5, 1.0]],
        separators=(",", ":"),
    )

    assert action_signature(frame) == hashlib.sha256(canonical_json.encode()).hexdigest()


def test_refresh_comparison_reports_changes_and_suppresses_only_fixed_numeric_noise() -> None:
    cached = _canonical_frame(["2024-01-02", "2024-01-03"])
    refreshed = cached.copy(deep=True)
    refreshed.loc[0, "open"] += 5e-12
    refreshed.loc[0, "volume"] = 101.0
    refreshed.loc[1, "dividend_amount"] = 0.25
    refreshed.loc[1, "split_coefficient"] = 2.0

    revisions = compare_cached_and_refreshed(cached, refreshed)

    assert [(item.timestamp, item.field) for item in revisions] == [
        (pd.Timestamp("2024-01-02"), "volume"),
        (pd.Timestamp("2024-01-03"), "dividend_amount"),
        (pd.Timestamp("2024-01-03"), "split_coefficient"),
    ]


def test_complete_result_has_exact_order_dtypes_and_defensive_frame_copy() -> None:
    frame = _canonical_frame(["2024-01-02", "2024-01-03"])
    result = evaluate_complete_request(
        frame,
        _request("2024-01-02", "2024-01-03"),
        pd.DatetimeIndex(["2024-01-02", "2024-01-03"]),
    )

    assert result.frame is not None
    assert tuple(result.frame.columns) == CANONICAL_COLUMNS
    assert result.frame.dtypes.astype(str).tolist() == [
        "datetime64[ns]",
        "string",
        "float64",
        "float64",
        "float64",
        "float64",
        "float64",
        "float64",
        "float64",
        "float64",
        "string",
    ]
    exposed = result.frame
    exposed.loc[0, "close"] = -1.0
    assert result.frame.loc[0, "close"] == 10.5


def _canonical_frame(timestamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(timestamps, dtype="datetime64[ns]"),
            "symbol": pd.Series(["AAPL"] * len(timestamps), dtype="string"),
            "open": pd.Series([10.0] * len(timestamps), dtype="float64"),
            "high": pd.Series([11.0] * len(timestamps), dtype="float64"),
            "low": pd.Series([9.0] * len(timestamps), dtype="float64"),
            "close": pd.Series([10.5] * len(timestamps), dtype="float64"),
            "volume": pd.Series([100.0] * len(timestamps), dtype="float64"),
            "adj_close": pd.Series([10.25] * len(timestamps), dtype="float64"),
            "dividend_amount": pd.Series([0.0] * len(timestamps), dtype="float64"),
            "split_coefficient": pd.Series([1.0] * len(timestamps), dtype="float64"),
            "source": pd.Series([Provider.YFINANCE.value] * len(timestamps), dtype="string"),
        }
    )
