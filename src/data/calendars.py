"""Closed calendar boundary for daily market-data session calculations."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from importlib.metadata import version
from types import MappingProxyType
from typing import Protocol

import pandas as pd
import pandas_market_calendars as market_calendars

from src.data.contracts import DEFAULT_CALENDAR, InvalidRequestError


class MarketCalendar(Protocol):
    """Exchange-session dependency injected into acquisition and quality code."""

    calendar_id: str

    def expected_sessions(
        self,
        start: date | datetime,
        end: date | datetime,
    ) -> pd.DatetimeIndex: ...

    def version_evidence(self) -> Mapping[str, str]: ...

    def session_closes(
        self,
        start: date | datetime,
        end: date | datetime,
    ) -> Mapping[pd.Timestamp, datetime]: ...


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


class XNYSCalendar:
    """Pandas-market-calendars implementation for the only V1 exchange."""

    calendar_id = DEFAULT_CALENDAR

    def __init__(self) -> None:
        self._calendar = market_calendars.get_calendar(self.calendar_id)

    def expected_sessions(self, start: date | datetime, end: date | datetime) -> pd.DatetimeIndex:
        start_date = _as_date(start)
        end_date = _as_date(end)
        if start_date > end_date:
            raise InvalidRequestError("start must not be after end")
        schedule = self._calendar.schedule(start_date=start_date, end_date=end_date)
        sessions = pd.DatetimeIndex(pd.to_datetime(schedule.index))
        if sessions.tz is not None:
            sessions = sessions.tz_localize(None)
        return sessions.normalize()

    def version_evidence(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "calendar": self.calendar_id,
                "calendar_version": version("pandas_market_calendars"),
                "pandas_market_calendars": version("pandas_market_calendars"),
            }
        )

    def session_closes(
        self,
        start: date | datetime,
        end: date | datetime,
    ) -> Mapping[pd.Timestamp, datetime]:
        """Return normalized session labels mapped to scheduled UTC closes."""
        schedule = self._calendar.schedule(
            start_date=_as_date(start),
            end_date=_as_date(end),
        )
        return MappingProxyType(
            {
                pd.Timestamp(label).tz_localize(None).normalize(): pd.Timestamp(close)
                .tz_convert(UTC)
                .to_pydatetime()
                for label, close in schedule["market_close"].items()
            }
        )


_CALENDAR_REGISTRY: Mapping[str, type[MarketCalendar]] = {DEFAULT_CALENDAR: XNYSCalendar}


def get_market_calendar(calendar_id: str = DEFAULT_CALENDAR) -> MarketCalendar:
    """Resolve a registered calendar without arbitrary imports or dispatch."""
    try:
        return _CALENDAR_REGISTRY[calendar_id]()
    except KeyError as error:
        raise InvalidRequestError(f"unsupported calendar {calendar_id!r}") from error


def group_contiguous_sessions(
    expected_sessions: pd.DatetimeIndex,
    missing_sessions: pd.DatetimeIndex,
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    """Group gaps according to adjacent exchange sessions, not elapsed days."""
    expected = pd.DatetimeIndex(expected_sessions).normalize()
    missing = set(pd.DatetimeIndex(missing_sessions).normalize())
    groups: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    group_start: pd.Timestamp | None = None
    previous: pd.Timestamp | None = None

    for session in expected:
        if session in missing:
            if group_start is None:
                group_start = session
            previous = session
        elif group_start is not None and previous is not None:
            groups.append((group_start, previous))
            group_start = None
            previous = None
    if group_start is not None and previous is not None:
        groups.append((group_start, previous))
    return tuple(groups)
