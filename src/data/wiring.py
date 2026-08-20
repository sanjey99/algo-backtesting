"""Production composition for the canonical market-data acquisition service."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from src.data.acquisition import AcquisitionService
from src.data.calendars import get_market_calendar
from src.data.contracts import Provider
from src.data.manifest import ManifestRepository
from src.data.providers import AlphaVantageProvider, YFinanceProvider
from src.data.retry import RetryExecutor
from src.data.store import DataStore


@lru_cache(maxsize=1)
def get_acquisition_service() -> AcquisitionService:
    """Return the process-shared acquisition service used by application entry points."""
    return create_acquisition_service()


def create_acquisition_service(
    *,
    cache_dir: str | Path = "data/raw",
    manifest_dir: str | Path = "data/acquisition-reports",
) -> AcquisitionService:
    """Wire the production acquisition effects behind one injectable dependency."""
    calendar = get_market_calendar()
    repository = ManifestRepository(manifest_dir)
    store = DataStore(
        cache_dir,
        calendar_versions=calendar.version_evidence(),
        manifest_repository=repository,
    )

    def clock() -> datetime:
        return datetime.now(UTC)

    key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    entitled = os.environ.get("ALPHA_VANTAGE_ADJUSTED_DAILY_ENTITLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    return AcquisitionService(
        store=store,
        manifest_repository=repository,
        calendar=calendar,
        provider_factories={
            Provider.YFINANCE: YFinanceProvider,
            Provider.ALPHA_VANTAGE: lambda: AlphaVantageProvider(
                api_key=key,
                adjusted_daily_entitled=entitled,
            ),
        },
        retry_executor=RetryExecutor(clock=clock),
        clock=clock,
    )
