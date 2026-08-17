"""Closed, redacted JSON logging utilities for runtime entry points.

This module is deliberately inert at import time. Applications opt into the
root handler by calling :func:`configure_logging` from their runtime boundary.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Final
from urllib.parse import parse_qsl, unquote_plus, urlsplit

_DEFAULT_EVENT: Final = "log.record"
_REDACTED: Final = "[REDACTED]"
_SECRET_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {"api_key", "authorization", "token", "password", "secret", "cookie"}
)
_URL_WITH_AUTHORITY: Final = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def _is_secret_field_name(name: str) -> bool:
    return name.casefold() in _SECRET_FIELD_NAMES


def _has_secret_query_parameter(query: str) -> bool:
    """Return whether a URL query contains an exact secret-shaped key."""
    for name, _ in parse_qsl(query, keep_blank_values=True):
        if _is_secret_field_name(name):
            return True

    # ``parse_qsl`` intentionally treats semicolons as data on modern Python.
    # Treat them as separators too so malformed legacy URLs fail closed.
    for component in query.split(";"):
        name, separator, _ = component.partition("=")
        if separator and _is_secret_field_name(unquote_plus(name)):
            return True
    return False


def _is_credential_bearing_url(value: str) -> bool:
    """Detect URL userinfo and secret-shaped query parameters conservatively."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        # A malformed authority with userinfo is still safer to redact than emit.
        return bool(_URL_WITH_AUTHORITY.match(value) and "@" in value)

    if parsed.username is not None:
        return True
    if parsed.query and _has_secret_query_parameter(parsed.query):
        return True
    return False


def _safe_value(value: object) -> None | bool | int | float | str:
    """Convert a field value to a JSON-safe scalar without calling ``repr``."""
    if isinstance(value, Enum):
        return _safe_value(value.value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _REDACTED if _is_credential_bearing_url(value) else value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    return type(value).__name__


def _sanitize_fields(fields: Mapping[str, object]) -> dict[str, None | bool | int | float | str]:
    """Produce a fresh, safe field mapping for one logging record."""
    return {
        name: _REDACTED if _is_secret_field_name(name) else _safe_value(value)
        for name, value in fields.items()
    }


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Emit a structured event while preserving a caplog-friendly record contract."""
    event_fields = _sanitize_fields(fields)
    logger.log(level, event, extra={"event": event, "event_fields": event_fields})


class JsonFormatter(logging.Formatter):
    """Format only approved LogRecord data as one redacted JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        raw_fields = getattr(record, "event_fields", {})
        fields = _sanitize_fields(raw_fields) if isinstance(raw_fields, Mapping) else {}
        raw_event = getattr(record, "event", _DEFAULT_EVENT)
        event = raw_event if isinstance(raw_event, str) else _safe_value(raw_event)

        payload: dict[str, object] = dict(fields)
        payload.update(
            {
                "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "level": record.levelname,
                "logger": record.name,
                "event": event,
            }
        )
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__

        return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def configure_logging(level: str | None = None) -> None:
    """Install one JSON root handler when an application explicitly opts in."""
    root = logging.getLogger()
    if any(getattr(handler, "_algo_json_handler", False) for handler in root.handlers):
        return

    selected = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric_level = logging.getLevelNamesMapping().get(selected, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._algo_json_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(numeric_level)
