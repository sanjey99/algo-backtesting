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


def test_log_event_rejects_non_identifier_event_names() -> None:
    """Using arbitrary messages as events would let credentials bypass redaction."""
    logger = logging.getLogger("test.observability.invalid_event")

    with pytest.raises(ValueError, match="stable identifier"):
        log_event(logger, logging.INFO, "https://metadata-user:metadata-pass@logs.example")


def test_formatter_redacts_credential_urls_from_event_logger_and_level() -> None:
    """Formatting event, logger, or level URL strings verbatim would bypass redaction."""
    metadata_url = "https://metadata-user:metadata-pass@logs.example"
    record = logging.LogRecord(
        name=metadata_url,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unstructured record",
        args=(),
        exc_info=None,
    )
    record.event = metadata_url
    record.levelname = metadata_url

    output = json.loads(JsonFormatter().format(record))

    assert output["event"] == "[REDACTED]"
    assert output["logger"] == "[REDACTED]"
    assert output["level"] == "[REDACTED]"
    assert "metadata-pass" not in json.dumps(output)


def test_formatter_redacts_credential_bearing_exception_type_names() -> None:
    """Exception type names must not bypass metadata sanitization."""
    error_type = type(
        "https://metadata-user:metadata-pass@errors.example",
        (Exception,),
        {},
    )
    error = error_type()
    record = logging.LogRecord(
        name="test.observability.exception_name",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="unstructured record",
        args=(),
        exc_info=(error_type, error, None),
    )

    output = json.loads(JsonFormatter().format(record))

    assert output["exception"] == "[REDACTED]"
    assert "metadata-pass" not in json.dumps(output)


def test_log_event_rejects_reserved_output_fields() -> None:
    """A caller-provided exception field would impersonate formatter exception context."""
    logger = logging.getLogger("test.observability.reserved_field")

    with pytest.raises(ValueError, match="reserved"):
        log_event(logger, logging.INFO, "backtest.failed", **{"exception": "caller-supplied"})


def test_log_event_rejects_non_identifier_field_names() -> None:
    """Credential-bearing field keys must not enter the stable JSON field namespace."""
    logger = logging.getLogger("test.observability.invalid_field")

    with pytest.raises(ValueError, match="stable identifier"):
        log_event(
            logger,
            logging.INFO,
            "backtest.failed",
            **{"https://metadata-user:metadata-pass@fields.example": "field-value"},
        )


def test_formatter_filters_unsafe_external_field_names() -> None:
    """Externally constructed records must not expose arbitrary field keys."""
    field_name = "https://metadata-user:metadata-pass@fields.example"
    record = logging.LogRecord(
        name="test.observability.external_field",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="unstructured record",
        args=(),
        exc_info=None,
    )
    record.event_fields = {field_name: "field-value", "run_id": "run-1"}

    output = json.loads(JsonFormatter().format(record))

    assert field_name not in output
    assert output["run_id"] == "run-1"
    assert "metadata-pass" not in json.dumps(output)


@pytest.mark.parametrize(
    "url",
    [
        " \nhttps://metadata-user:metadata-pass@[invalid",
        "\thttps://metadata-user:metadata-pass@[invalid",
    ],
)
def test_whitespace_prefixed_malformed_credential_urls_fail_closed(url: str) -> None:
    """Whitespace before malformed URL authority must not bypass conservative redaction."""
    output = _format_event("acquisition.requested", endpoint=url)

    assert output["endpoint"] == "[REDACTED]"
    assert "metadata-pass" not in json.dumps(output)


def test_configure_logging_is_idempotent() -> None:
    """Adding a handler per call would duplicate every emitted log line."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    original_level = root.level
    created_handlers: list[logging.Handler] = []
    try:
        configure_logging("DEBUG")
        configure_logging("DEBUG")
        marked_handlers = [
            handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)
        ]
        created_handlers = [handler for handler in root.handlers if handler not in before_handlers]

        assert len(marked_handlers) == 1
        assert isinstance(marked_handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:] = before_handlers
        for handler in created_handlers:
            handler.close()
        root.setLevel(original_level)


def test_configure_logging_falls_back_to_info_for_unsupported_level() -> None:
    """An invalid level uses INFO without disturbing an earlier process handler."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    created_handlers: list[logging.Handler] = []
    try:
        # API/dashboard startup may already have installed this process-wide handler.
        # Detach it without closing it so this test always exercises fresh configuration.
        root.handlers[:] = [
            handler
            for handler in before_handlers
            if not getattr(handler, "_algo_json_handler", False)
        ]
        root.setLevel(logging.WARNING)
        configure_logging("NOT_A_LEVEL")
        marked_handlers = [
            handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)
        ]
        created_handlers = [handler for handler in root.handlers if handler not in before_handlers]

        assert len(marked_handlers) == 1
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = before_handlers
        for handler in created_handlers:
            handler.close()
        root.setLevel(before_level)


@pytest.mark.parametrize(
    ("level", "expected_level"),
    [("", logging.INFO), (None, logging.DEBUG)],
    ids=("explicit-empty-falls-back-to-info", "none-inherits-environment"),
)
def test_configure_logging_distinguishes_empty_override_from_none(
    level: str | None,
    expected_level: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``None`` delegates logging-level selection to the environment."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    created_handlers: list[logging.Handler] = []
    try:
        root.handlers[:] = [
            handler
            for handler in before_handlers
            if not getattr(handler, "_algo_json_handler", False)
        ]
        root.setLevel(logging.WARNING)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        configure_logging(level)

        marked_handlers = [
            handler for handler in root.handlers if getattr(handler, "_algo_json_handler", False)
        ]
        created_handlers = [handler for handler in root.handlers if handler not in before_handlers]

        assert len(marked_handlers) == 1
        assert root.level == expected_level
    finally:
        root.handlers[:] = before_handlers
        for handler in created_handlers:
            handler.close()
        root.setLevel(before_level)
