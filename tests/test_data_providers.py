from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.data.contracts import (
    AcquisitionRequest,
    ActionCoverage,
    ProviderEntitlementError,
    ProviderQuotaError,
    ProviderSchemaError,
)
from src.data.providers import (
    AlphaVantageProvider,
    YFinanceProvider,
    plan_provider_candidates,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "market_data"


def _request(start: date = date(2024, 1, 2), end: date = date(2024, 1, 3)) -> AcquisitionRequest:
    return AcquisitionRequest("AAPL", start, end)


def _fixture_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text())


def _test_api_key() -> str:
    return "test-api-key"


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


def test_yfinance_retains_native_frame_requests_actions_and_translates_inclusive_end() -> None:
    raw = pd.read_csv(FIXTURE_DIR / "yfinance_flat.csv", index_col="Date", parse_dates=True)
    calls: list[dict[str, Any]] = []

    def download(*args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append({"args": args, "kwargs": kwargs})
        return raw

    provider = YFinanceProvider(download=download, now=lambda: datetime(2024, 1, 4, tzinfo=UTC))
    batch = provider.fetch(_request())

    assert batch.frame is raw
    assert batch.raw_row_count == 3
    assert batch.action_coverage is ActionCoverage.REPRESENTED
    assert calls == [
        {
            "args": ("AAPL",),
            "kwargs": {
                "start": date(2024, 1, 2),
                "end": date(2024, 1, 4),
                "interval": "1d",
                "actions": True,
                "auto_adjust": False,
                "progress": False,
            },
        }
    ]
    assert provider.capabilities.supports_actions is True
    assert provider.capabilities.supported_intervals == frozenset({"1d"})
    assert "apikey" not in str(batch.to_dict()).lower()


def test_yfinance_accepts_multiindex_provider_shape_without_normalizing_it() -> None:
    raw = pd.read_csv(FIXTURE_DIR / "yfinance_flat.csv", index_col="Date", parse_dates=True)
    raw.columns = pd.MultiIndex.from_tuples([(column, "AAPL") for column in raw.columns])
    provider = YFinanceProvider(download=lambda *_, **__: raw)

    batch = provider.fetch(_request())

    assert isinstance(batch.frame.columns, pd.MultiIndex)
    assert batch.frame is raw


def test_yfinance_rejects_empty_or_unrecognizable_provider_frames() -> None:
    empty = YFinanceProvider(download=lambda *_, **__: pd.DataFrame())
    malformed = YFinanceProvider(download=lambda *_, **__: pd.DataFrame({"price": [1.0]}))

    with pytest.raises(ProviderSchemaError):
        empty.fetch(_request())
    with pytest.raises(ProviderSchemaError):
        malformed.fetch(_request())


@pytest.mark.parametrize(
    ("fixture", "error_type"),
    [
        ("alpha_error.json", ProviderSchemaError),
        ("alpha_note.json", ProviderQuotaError),
        ("alpha_information.json", ProviderEntitlementError),
        ("alpha_malformed.json", ProviderSchemaError),
    ],
)
def test_alpha_maps_provider_envelopes_to_typed_failures(
    fixture: str, error_type: type[Exception]
) -> None:
    provider = AlphaVantageProvider(
        api_key=_test_api_key(),
        adjusted_daily_entitled=True,
        http_get=lambda *_, **__: _Response(_fixture_json(fixture)),
    )

    with pytest.raises(error_type):
        provider.fetch(_request())


def test_alpha_records_output_size_and_action_fields_without_credentials() -> None:
    seen_params: list[dict[str, str]] = []

    def http_get(*_: Any, **kwargs: Any) -> _Response:
        seen_params.append(kwargs["params"])
        return _Response(_fixture_json("alpha_daily_adjusted.json"))

    provider = AlphaVantageProvider(
        api_key=_test_api_key(),
        adjusted_daily_entitled=True,
        output_size="full",
        http_get=http_get,
        now=lambda: datetime(2024, 1, 4, tzinfo=UTC),
    )
    batch = provider.fetch(_request())

    assert batch.frame.columns.tolist()[-2:] == ["7. dividend amount", "8. split coefficient"]
    assert batch.action_coverage is ActionCoverage.REPRESENTED
    assert provider.capabilities.output_size == "full"
    assert provider.capabilities.supports_full_history is True
    assert batch.response_metadata["output_size"] == "full"
    assert "test-api-key" not in str(batch.to_dict())
    assert seen_params == [
        {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": "AAPL",
            "outputsize": "full",
            "datatype": "json",
            "apikey": "test-api-key",
        }
    ]


def test_alpha_requires_key_and_explicit_entitlement_before_http() -> None:
    calls = 0

    def http_get(**_: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(_fixture_json("alpha_daily_adjusted.json"))

    provider = AlphaVantageProvider(api_key=None, adjusted_daily_entitled=False, http_get=http_get)
    eligibility = provider.eligibility(_request())

    assert eligibility.eligible is False
    assert eligibility.reason == "missing API key"
    with pytest.raises(ProviderEntitlementError):
        provider.fetch(_request())
    assert calls == 0


def test_alpha_does_not_mutate_parameters_and_rejects_non_daily_before_http() -> None:
    calls = 0

    def http_get(**_: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(_fixture_json("alpha_daily_adjusted.json"))

    provider = AlphaVantageProvider(
        api_key=_test_api_key(), adjusted_daily_entitled=True, http_get=http_get
    )
    params = {"symbol": "AAPL"}

    with pytest.raises(ProviderSchemaError):
        provider.request(params, interval="1h")

    assert params == {"symbol": "AAPL"}
    assert calls == 0


def test_candidate_planning_skips_compact_alpha_when_coverage_cannot_reach_request() -> None:
    calls = 0

    def http_get(**_: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(_fixture_json("alpha_daily_adjusted.json"))

    compact = AlphaVantageProvider(
        api_key=_test_api_key(),
        adjusted_daily_entitled=True,
        output_size="compact",
        compact_lookback_days=100,
        today=lambda: date(2024, 6, 1),
        http_get=http_get,
    )
    plan = plan_provider_candidates((compact,), _request(date(2024, 1, 2), date(2024, 1, 3)))

    assert plan.eligible == ()
    assert plan.skipped[0].reason == "compact output cannot cover requested history"
    assert calls == 0
