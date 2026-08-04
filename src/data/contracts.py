"""Typed, JSON-safe contracts for daily market-data acquisition.

The contracts in this module deliberately contain no transport behaviour.  They
are the boundary shared by providers, quality evaluation, cache publication,
and the API/CLI adapters.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

CONTRACT_VERSION = "1"
DEFAULT_CALENDAR = "XNYS"
DEFAULT_INTERVAL = "1d"
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|x-api-key|authorization|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|cookie|credential)",
    re.IGNORECASE,
)
_INLINE_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|x-api-key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"authorization|cookie)\b(\s*[=:]\s*)([^\s,;&]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;&]+")
_REDACTED = "[REDACTED]"


class Provider(StrEnum):
    """Providers supported by the V1 adapter registry."""

    YFINANCE = "yfinance"
    ALPHA_VANTAGE = "alpha_vantage"


class SourcePreference(StrEnum):
    """A caller's requested provider policy."""

    AUTO = "auto"
    YFINANCE = Provider.YFINANCE.value
    ALPHA_VANTAGE = Provider.ALPHA_VANTAGE.value


class AcquisitionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class CacheStatus(StrEnum):
    MISS = "miss"
    FULL_HIT = "full_hit"
    PARTIAL_HIT = "partial_hit"
    STALE_REFRESH = "stale_refresh"
    FORCED_REFRESH = "forced_refresh"
    INVALIDATED = "invalidated"


class QualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    FATAL = "fatal"


class ActionCoverage(StrEnum):
    REPRESENTED = "represented"
    NOT_REPRESENTED = "not_represented"
    UNKNOWN = "unknown"


class DataAcquisitionError(Exception):
    """Base error for the acquisition subsystem."""


class InvalidRequestError(DataAcquisitionError):
    """A request fails validation before it is admitted for acquisition."""


class ContractViolationError(DataAcquisitionError):
    """A candidate or persisted artifact violates a declared contract."""


class ProviderError(DataAcquisitionError):
    """Base error returned by a provider adapter."""


class TransientProviderError(ProviderError):
    """A provider failure that may be retried under the retry policy."""


class ProviderAuthenticationError(ProviderError):
    """A provider rejected authentication."""


class ProviderEntitlementError(ProviderError):
    """A provider account lacks the requested data entitlement."""


class ProviderQuotaError(ProviderError):
    """A provider quota prevents this attempt."""


class ProviderSchemaError(ProviderError):
    """A provider response cannot be mapped safely."""


class NoUsableDataError(DataAcquisitionError):
    """No provider candidate supplied usable data."""


class QualityError(DataAcquisitionError):
    """A candidate fails fatal quality validation."""


class CacheError(DataAcquisitionError):
    """Base error for canonical cache operations."""


class CachePublicationError(CacheError):
    """An immutable cache generation could not be published."""


class ManifestError(DataAcquisitionError):
    """A manifest could not be serialized or stored."""


class ConcurrentPublicationError(CachePublicationError):
    """Repeated optimistic cache publication conflicts exhausted retries."""


def _coerce_date(value: date | datetime, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise InvalidRequestError(f"{field_name} must be a date or datetime")


def _coerce_enum(value: Any, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        allowed = ", ".join(item.value for item in enum_type)
        message = f"unsupported {field_name!r}; expected one of: {allowed}"
        raise InvalidRequestError(message) from error


def _is_sensitive_key(key: object) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(str(key)))


def _redact_text(value: str) -> str:
    """Remove credentials from free-form errors and credential-bearing URLs."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed is not None and (parsed.scheme or parsed.netloc) and parsed.query:
        query = urlencode(
            [
                (key, _REDACTED if _is_sensitive_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = f"{_REDACTED}@{netloc.rsplit('@', maxsplit=1)[1]}"
        value = urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    value = _BEARER_PATTERN.sub(f"Bearer {_REDACTED}", value)
    return _INLINE_CREDENTIAL_PATTERN.sub(rf"\1\2{_REDACTED}", value)


def _freeze_metadata(value: Any) -> Any:
    """Defensively freeze and redact arbitrary evidence captured at a boundary."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _REDACTED if _is_sensitive_key(key) else _freeze_metadata(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, tuple | list):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, frozenset | set):
        return frozenset(_freeze_metadata(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    """A syntactically admitted, inclusive daily acquisition request."""

    symbol: str
    start: date | datetime
    end: date | datetime
    interval: str = DEFAULT_INTERVAL
    calendar: str = DEFAULT_CALENDAR
    source: SourcePreference | str = SourcePreference.AUTO
    use_cache: bool = True
    refresh: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise InvalidRequestError("symbol must be a string")
        symbol = self.symbol.strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise InvalidRequestError("symbol must use the safe V1 symbol grammar")
        start = _coerce_date(self.start, "start")
        end = _coerce_date(self.end, "end")
        if start > end:
            raise InvalidRequestError("start must not be after end")
        if self.interval != DEFAULT_INTERVAL:
            raise InvalidRequestError("V1 supports interval '1d' only")
        if self.calendar != DEFAULT_CALENDAR:
            raise InvalidRequestError("V1 supports calendar 'XNYS' only")
        if not isinstance(self.use_cache, bool) or not isinstance(self.refresh, bool):
            raise InvalidRequestError("use_cache and refresh must be boolean")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "source", _coerce_enum(self.source, SourcePreference, "source"))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json_safe(self))


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Quality thresholds applied after complete requested-range assembly."""

    minimum_coverage: float = 0.98
    max_consecutive_missing_sessions: int = 2
    action_relative_tolerance: float = 1e-9
    action_absolute_tolerance: float = 1e-12

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_coverage <= 1.0:
            raise ValueError("minimum_coverage must be between zero and one")
        if self.max_consecutive_missing_sessions < 0:
            raise ValueError("max_consecutive_missing_sessions must be non-negative")
        if self.action_relative_tolerance < 0 or self.action_absolute_tolerance < 0:
            raise ValueError("action tolerances must be non-negative")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential full-jitter retry settings."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    max_retry_after_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delays must be non-negative and capped above the base delay")
        if self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: Provider
    supported_intervals: frozenset[str] = frozenset({DEFAULT_INTERVAL})
    supports_actions: bool = False
    requires_api_key: bool = False
    supports_full_history: bool = True


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    """Provider-native data retained in-process for normalization only."""

    provider: Provider
    request: AcquisitionRequest
    frame: pd.DataFrame = field(repr=False, compare=False)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    native_timezone: str | None = None
    raw_row_count: int = 0
    response_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    action_coverage: ActionCoverage = ActionCoverage.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "response_metadata", _freeze_metadata(self.response_metadata))

    def to_dict(self) -> dict[str, Any]:
        """Return audit metadata without leaking or serializing the frame."""
        return {
            "provider": self.provider.value,
            "request": self.request.to_dict(),
            "received_at": json_safe(self.received_at),
            "native_timezone": self.native_timezone,
            "raw_row_count": self.raw_row_count,
            "response_metadata": json_safe(self.response_metadata),
            "action_coverage": self.action_coverage.value,
        }


@dataclass(frozen=True, slots=True)
class QualityFinding:
    severity: QualitySeverity
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _redact_text(self.message))
        object.__setattr__(self, "details", _freeze_metadata(self.details))


@dataclass(frozen=True, slots=True)
class RejectedRow:
    source_row_number: int
    reason: str
    timestamp: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", _redact_text(self.reason))
        object.__setattr__(self, "details", _freeze_metadata(self.details))


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    provider: Provider
    attempt_number: int
    started_at: datetime
    duration_seconds: float
    outcome: str
    retry_delay_seconds: float | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.error_message is not None:
            object.__setattr__(self, "error_message", _redact_text(self.error_message))


@dataclass(frozen=True, slots=True)
class CacheEvidence:
    status: CacheStatus
    generation_id: str | None = None
    cache_key: str | None = None
    compatibility_reason: str | None = None
    covered_sessions: int = 0
    missing_sessions: int = 0

    def __post_init__(self) -> None:
        if self.compatibility_reason is not None:
            object.__setattr__(
                self,
                "compatibility_reason",
                _redact_text(self.compatibility_reason),
            )


@dataclass(frozen=True, slots=True)
class LineageSegment:
    start: date
    end: date
    provider: Provider
    acquired_at: datetime
    action_coverage: ActionCoverage
    content_hash: str
    action_signature: str


@dataclass(frozen=True, slots=True)
class AcquisitionManifest:
    """Versioned JSON-safe evidence for one admitted acquisition request."""

    acquisition_id: str
    request: AcquisitionRequest
    status: AcquisitionStatus
    schema_version: str = CONTRACT_VERSION
    quality_policy: QualityPolicy = field(default_factory=QualityPolicy)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    environment_versions: Mapping[str, str] = field(default_factory=dict)
    cache: CacheEvidence = field(default_factory=lambda: CacheEvidence(CacheStatus.MISS))
    attempts: tuple[AttemptEvidence, ...] = ()
    findings: tuple[QualityFinding, ...] = ()
    rejected_rows: tuple[RejectedRow, ...] = ()
    lineage: tuple[LineageSegment, ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)
    coverage: float | None = None
    output_hash: str | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment_versions",
            _freeze_metadata(self.environment_versions),
        )
        object.__setattr__(self, "counters", _freeze_metadata(self.counters))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json_safe(self))


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """A validated frame with its immutable acquisition evidence."""

    frame: pd.DataFrame = field(repr=False, compare=False)
    manifest: AcquisitionManifest

    def to_dict(self) -> dict[str, Any]:
        """Results never serialize canonical frames into manifest/API JSON."""
        return {"manifest": self.manifest.to_dict()}


def json_safe(value: Any) -> Any:
    """Convert contract values to deterministic JSON-compatible primitives."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, pd.DataFrame):
        raise TypeError("pandas frames are in-process values and cannot be serialized")
    if hasattr(value, "__dataclass_fields__"):
        return {item.name: json_safe(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive_key(key) else json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        return [json_safe(item) for item in value]
    return value
