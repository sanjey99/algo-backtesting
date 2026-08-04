"""Concurrent acquisition publication and range-scoped rebase integration tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    ActionCoverage,
    LineageSegment,
    Provider,
)
from tests.test_data_acquisition import NOW, SESSIONS, canonical_frame, service


def test_rebase_overlays_only_this_writers_fetched_ranges(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 2), date(2024, 1, 10))
    _, store, _ = service(tmp_path, {})
    base = canonical_frame(request, SESSIONS)

    def manifest(identity: str) -> AcquisitionManifest:
        return AcquisitionManifest(
            identity,
            request,
            AcquisitionStatus.SUCCESS,
            lineage=(
                LineageSegment(
                    request.start,
                    request.end,
                    Provider.YFINANCE,
                    NOW,
                    ActionCoverage.REPRESENTED,
                    "a" * 64,
                    "actions",
                ),
            ),
            started_at=NOW,
            completed_at=NOW,
        )

    first = store.publish_generation(request, base, {}, manifest("base"))
    latest = base.copy(deep=True)
    latest.loc[0, ["open", "high", "low", "close", "adj_close"]] = [
        50.0,
        51.0,
        49.0,
        50.5,
        50.5,
    ]
    store.publish_generation(
        request,
        latest,
        {},
        manifest("latest"),
        base_generation_id=first.generation_id,
    )
    stale = base.copy(deep=True)
    stale.loc[3, ["open", "high", "low", "close", "adj_close"]] = [
        80.0,
        81.0,
        79.0,
        80.5,
        80.5,
    ]

    published = store.publish_generation(
        request,
        stale,
        {},
        manifest("stale"),
        base_generation_id=first.generation_id,
        replace_ranges=((date(2024, 1, 5), date(2024, 1, 5)),),
    )

    assert published.frame.loc[0, "open"] == 50.0
    assert published.frame.loc[3, "open"] == 80.0
