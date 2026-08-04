"""Table-driven tests for provider-native to canonical normalization."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.data.contracts import AcquisitionRequest, ActionCoverage, Provider, ProviderBatch
from src.data.normalization import CANONICAL_COLUMNS, normalize_provider_batch

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market_data"


def _request(start: date = date(2024, 1, 2), end: date = date(2024, 1, 3)) -> AcquisitionRequest:
    return AcquisitionRequest("AAPL", start, end)


def _batch(
    provider: Provider,
    frame: pd.DataFrame,
    *,
    request: AcquisitionRequest | None = None,
    timezone: str | None = None,
    action_coverage: ActionCoverage = ActionCoverage.REPRESENTED,
) -> ProviderBatch:
    return ProviderBatch(
        provider,
        request or _request(),
        frame,
        native_timezone=timezone,
        raw_row_count=len(frame),
        action_coverage=action_coverage,
    )


@pytest.mark.parametrize("provider", [Provider.YFINANCE, Provider.ALPHA_VANTAGE])
def test_maps_each_provider_shape_to_the_exact_internal_candidate_shape(provider: Provider) -> None:
    if provider is Provider.YFINANCE:
        frame = pd.read_csv(FIXTURE_DIR / "yfinance_flat.csv", index_col="Date")
    else:
        payload: dict[str, Any] = json.loads(
            (FIXTURE_DIR / "alpha_daily_adjusted.json").read_text()
        )
        frame = pd.DataFrame.from_dict(payload["Time Series (Daily)"], orient="index")

    result = normalize_provider_batch(_batch(provider, frame))

    assert result.is_fatal is False
    assert tuple(result.candidate_frame.columns) == (*CANONICAL_COLUMNS, "_source_row_number")
    assert result.candidate_frame["timestamp"].dtype == "datetime64[ns]"
    assert result.candidate_frame["symbol"].dtype == "string"
    assert result.candidate_frame["source"].dtype == "string"
    assert all(
        result.candidate_frame[column].dtype == "float64"
        for column in CANONICAL_COLUMNS[2:-1]
    )


def test_normalizes_naive_and_aware_daily_timestamps_stably_and_filters_inclusive_range() -> None:
    frame = pd.DataFrame(
        {
            "Open": [30, 10, 20, 21, 40],
            "High": [31, 11, 22, 22, 41],
            "Low": [29, 9, 19, 19, 39],
            "Close": [30, 10, 21, 21, 40],
            "Adj Close": [30, 10, 21, 21, 40],
            "Volume": [300, 100, 200, 201, 400],
            "Dividends": [0, 0, 0, 0, 0],
            "Stock Splits": [1, 1, 1, 1, 1],
        },
        index=[
            "2024-01-01",
            "2024-01-03T09:30:00-05:00",
            "2024-01-02",
            "2024-01-02",
            "2024-01-04",
        ],
    )

    result = normalize_provider_batch(_batch(Provider.YFINANCE, frame))

    candidate = result.candidate_frame
    assert candidate["timestamp"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert candidate["_source_row_number"].tolist() == [2, 3, 1]
    assert result.counters.provider_rows == 5
    assert result.counters.out_of_range_rows == 2
    assert result.counters.in_range_rows == result.counters.dedupe_input_rows == 3


@pytest.mark.parametrize("bad_timestamp", [None, "not-a-date", "2024-03-10 02:30:00"])
def test_records_every_unclassifiable_timestamp_before_range_filtering(
    bad_timestamp: object,
) -> None:
    frame = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Adj Close": [10.5],
            "Volume": [100.0],
            "Dividends": [0.0],
            "Stock Splits": [1.0],
        },
        index=[bad_timestamp],
    )
    timezone = "America/New_York" if bad_timestamp == "2024-03-10 02:30:00" else None

    result = normalize_provider_batch(_batch(Provider.YFINANCE, frame, timezone=timezone))

    assert result.counters.provider_rows == 1
    assert result.counters.timestamp_unclassifiable_rows == 1
    assert result.counters.out_of_range_rows == 0
    assert result.counters.in_range_rows == 0
    assert result.timestamp_rejections[0].source_row_number == 0


def test_defaults_missing_action_columns_only_for_declared_represented_coverage() -> None:
    frame = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Adj Close": [10.5],
            "Volume": [100.0],
        },
        index=["2024-01-02"],
    )

    represented = normalize_provider_batch(_batch(Provider.YFINANCE, frame))
    unknown = normalize_provider_batch(
        _batch(Provider.YFINANCE, frame, action_coverage=ActionCoverage.UNKNOWN)
    )

    assert represented.candidate_frame.loc[0, "dividend_amount"] == 0.0
    assert represented.candidate_frame.loc[0, "split_coefficient"] == 1.0
    assert pd.isna(unknown.candidate_frame.loc[0, "dividend_amount"])
    assert pd.isna(unknown.candidate_frame.loc[0, "split_coefficient"])


def test_unusable_provider_schema_fails_closed_without_a_candidate() -> None:
    frame = pd.DataFrame({"Close": [10.0]}, index=["2024-01-02"])

    result = normalize_provider_batch(_batch(Provider.YFINANCE, frame))

    assert result.is_fatal is True
    assert result.candidate_frame.empty
    assert result.findings[0].code == "unusable_schema"


def test_normalization_result_does_not_expose_mutable_internal_frame() -> None:
    frame = pd.read_csv(FIXTURE_DIR / "yfinance_flat.csv", index_col="Date")
    result = normalize_provider_batch(_batch(Provider.YFINANCE, frame))

    exposed = result.candidate_frame
    exposed.loc[0, "open"] = -1.0

    assert result.candidate_frame.loc[0, "open"] != -1.0
