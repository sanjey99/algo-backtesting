"""Behavioral tests for the structured logging boundary."""

from __future__ import annotations

import io
import json
import logging
import math
from enum import Enum

import pytest

from src.observability import JsonFormatter, configure_logging, log_event


class _Severity(Enum):
    HIGH = "high"


def _format_event(event: str, **fields: object) -> dict[str, object]:
    """Format one real logging record without relying on root configuration."""
    stream = io.StringIO()
    logger = logging.getLogger(f"test.observability.{event}")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        log_event(logger, logging.INFO, event, **fields)
        return json.loads(stream.getvalue())
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_json_formatter_emits_stable_fields() -> None:
    """Removing the structured event properties would break downstream consumers."""
    output = _format_event("backtest.started", run_id="run-1", strategy="ma")

    assert output["event"] == "backtest.started"
    assert output["run_id"] == "run-1"
    assert output["level"] == "INFO"
    assert output["logger"] == "test.observability.backtest.started"
    assert isinstance(output["timestamp"], str)
    assert output["timestamp"].endswith("Z")


def test_log_event_attaches_sanitized_attributes_to_the_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Changing the LogRecord contract would break caplog and future adapters."""
    logger = logging.getLogger("test.observability.record")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, logging.INFO, "backtest.started", run_id="run-1", payload={"raw": True})

    record = caplog.records[-1]
    assert record.event == "backtest.started"
    assert record.event_fields == {"run_id": "run-1", "payload": "dict"}


def test_secret_shaped_fields_are_redacted() -> None:
    """Dropping exact-key redaction would expose credentials in formatted logs."""
    first_value = "redaction-fixture-one"
    second_value = "redaction-fixture-two"
    output = _format_event(
        "acquisition.failed",
        symbol="SPY",
        **{"api_key": first_value, "authorization": second_value},
    )

    encoded = json.dumps(output)
    assert first_value not in encoded
    assert second_value not in encoded
    assert output["api_key"] == "[REDACTED]"
    assert output["authorization"] == "[REDACTED]"


@pytest.mark.parametrize(
    "url",
    [
        "https://market-user:fixture-url-password@prices.example/v1",
        "https://prices.example/v1?api_key=fixture-query-secret",
        "postgresql://trader@db.example/positions",
    ],
)
def test_credential_bearing_urls_are_redacted(url: str) -> None:
    """Removing URL credential detection would leak credentials outside secret-key fields."""
    output = _format_event("acquisition.requested", endpoint=url)

    assert output["endpoint"] == "[REDACTED]"
    assert "fixture-url-password" not in json.dumps(output)
    assert "fixture-query-secret" not in json.dumps(output)


def test_external_exception_message_is_not_serialized() -> None:
    """Serializing exception text would leak untrusted provider diagnostics."""
    output = _format_event("backtest.failed", error=ValueError("fixture-sensitive-value"))

    assert output["error"] == "ValueError"
    assert "fixture-sensitive-value" not in json.dumps(output)


def test_formatter_emits_only_safe_scalar_and_enum_field_values() -> None:
    """Accepting arbitrary objects or non-finite numbers would make JSON logs unsafe."""
    output = _format_event(
        "backtest.metrics",
        none=None,
        flag=True,
        count=3,
        ratio=1.25,
        severity=_Severity.HIGH,
        nan=math.nan,
        infinity=math.inf,
        payload=["unstructured"],
    )

    assert output["none"] is None
    assert output["flag"] is True
    assert output["count"] == 3
    assert output["ratio"] == 1.25
    assert output["severity"] == "high"
    assert output["nan"] == "float"
    assert output["infinity"] == "float"
    assert output["payload"] == "list"


def test_formatter_serializes_only_the_exception_type_from_exc_info() -> None:
    """Formatting exception messages from exc_info would bypass field sanitization."""
    stream = io.StringIO()
    logger = logging.getLogger("test.observability.exc_info")
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        try:
            raise RuntimeError("fixture-exception-message")
        except RuntimeError:
            logger.exception("provider failed")
        output = json.loads(stream.getvalue())
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert output["event"] == "log.record"
    assert output["exception"] == "RuntimeError"
    assert "fixture-exception-message" not in json.dumps(output)


def test_configure_logging_adds_one_json_handler_and_uses_valid_level() -> None:
    """Adding a handler per call would duplicate every emitted log line."""
    root = logging.getLogger()
    original_level = root.level
    added_handlers: list[logging.Handler] = []
    try:
        configure_logging("DEBUG")
        added_handlers = [
            handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)
        ]
        configure_logging("ERROR")

        assert len(added_handlers) == 1
        assert root.level == logging.DEBUG
        assert isinstance(added_handlers[0].formatter, JsonFormatter)
    finally:
        for handler in added_handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(original_level)


def test_configure_logging_falls_back_to_info_for_unsupported_level() -> None:
    """Passing an unsupported level must not leave the root logger misconfigured."""
    root = logging.getLogger()
    original_level = root.level
    added_handlers: list[logging.Handler] = []
    try:
        configure_logging("NOT_A_LEVEL")
        added_handlers = [
            handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)
        ]

        assert len(added_handlers) == 1
        assert root.level == logging.INFO
    finally:
        for handler in added_handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(original_level)
