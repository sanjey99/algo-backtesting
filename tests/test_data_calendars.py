"""Tests for exchange-session calculations at the calendar boundary."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data.calendars import XNYSCalendar, group_contiguous_sessions


def test_xnys_sessions_exclude_weekends_and_known_holidays() -> None:
    calendar = XNYSCalendar()

    sessions = calendar.expected_sessions(date(2024, 7, 3), date(2024, 7, 8))

    assert list(sessions) == [
        pd.Timestamp("2024-07-03"),
        pd.Timestamp("2024-07-05"),
        pd.Timestamp("2024-07-08"),
    ]
    assert sessions.tz is None
    assert all(session == session.normalize() for session in sessions)


def test_missing_session_ranges_use_exchange_adjacency_not_calendar_days() -> None:
    expected = pd.DatetimeIndex(["2024-07-03", "2024-07-05", "2024-07-08", "2024-07-09"])
    missing = pd.DatetimeIndex(["2024-07-03", "2024-07-05", "2024-07-09"])

    assert group_contiguous_sessions(expected, missing) == (
        (pd.Timestamp("2024-07-03"), pd.Timestamp("2024-07-05")),
        (pd.Timestamp("2024-07-09"), pd.Timestamp("2024-07-09")),
    )


def test_calendar_exposes_calendar_and_dependency_versions() -> None:
    evidence = XNYSCalendar().version_evidence()

    assert evidence["calendar"] == "XNYS"
    assert evidence["calendar_version"]
    assert evidence["pandas_market_calendars"]
    with pytest.raises(TypeError):
        evidence["calendar"] = "changed"  # type: ignore[index]
