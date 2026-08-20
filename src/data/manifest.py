"""Immutable, redacted request-report persistence for admitted acquisitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from src.data.contracts import (
    CONTRACT_VERSION,
    AcquisitionManifest,
    AcquisitionRequest,
    ArtifactError,
    ManifestError,
    json_safe,
)
from src.data.durability import fsync_directory

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class AcquisitionAdmission:
    """Identity assigned only after syntactic request validation succeeds."""

    acquisition_id: str
    request: AcquisitionRequest
    admitted_at: datetime


class ManifestRepository:
    """An append-only V1 archive of deterministic acquisition reports."""

    def __init__(
        self,
        root: str | Path,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(root)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def root(self) -> Path:
        return self._root

    def admit(
        self,
        *,
        symbol: str,
        start: date | datetime,
        end: date | datetime,
        interval: str = "1d",
        calendar: str = "XNYS",
        source: str = "auto",
        use_cache: bool = True,
        refresh: bool = False,
    ) -> AcquisitionAdmission:
        """Validate a request before consuming an acquisition identifier."""
        request = AcquisitionRequest(
            symbol=symbol,
            start=start,
            end=end,
            interval=interval,
            calendar=calendar,
            source=source,
            use_cache=use_cache,
            refresh=refresh,
        )
        acquisition_id = self._validated_id(self._id_factory())
        admitted_at = self._utc_now()
        return AcquisitionAdmission(acquisition_id, request, admitted_at)

    def archive(self, manifest: AcquisitionManifest) -> Path:
        """Namespace-atomically append a complete report, rejecting identifier reuse."""
        if manifest.started_at is None or manifest.completed_at is None:
            raise ArtifactError("admitted reports require started_at and completed_at")
        return self.archive_document(manifest.acquisition_id, manifest.to_dict())

    def archive_document(self, acquisition_id: str, document: Mapping[str, Any]) -> Path:
        """Append a previously embedded, already-redacted manifest document."""
        safe_id = self._validated_id(acquisition_id)
        serializable = json_safe(document)
        if not isinstance(serializable, dict):
            raise ArtifactError("manifest document must be a JSON object")
        if serializable.get("acquisition_id") != safe_id:
            raise ArtifactError("manifest acquisition identifier does not match archive path")
        if serializable.get("schema_version") != CONTRACT_VERSION:
            raise ArtifactError("manifest schema version is incompatible")
        payload = _json_bytes(serializable)
        destination = self._path(safe_id)
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise ArtifactError("existing manifest archive is not a regular file")
            try:
                existing = destination.read_bytes()
            except OSError as error:
                raise ArtifactError("existing manifest archive cannot be read") from error
            if existing == payload:
                return destination
            raise ArtifactError("acquisition identifier already has a different archived report")

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.parent / f".{safe_id}.{uuid.uuid4().hex}.tmp"
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.read_bytes() != payload:
                        raise ArtifactError(
                            "acquisition identifier already has a different archived report"
                        ) from None
            finally:
                temporary.unlink(missing_ok=True)
            fsync_directory(destination.parent)
        except ArtifactError:
            raise
        except OSError as error:
            raise ArtifactError("manifest archive publication failed") from error
        return destination

    def lookup(self, acquisition_id: str) -> dict[str, Any] | None:
        """Return a defensive report document or ``None`` when not archived."""
        path = self._path(self._validated_id(acquisition_id))
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ManifestError("archived manifest is not a regular file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestError("archived manifest is corrupt") from error
        if not isinstance(value, dict) or value.get("acquisition_id") != acquisition_id:
            raise ManifestError("archived manifest violates its identifier contract")
        return cast(dict[str, Any], json.loads(json.dumps(value)))

    def write_optional_copy(
        self,
        manifest: AcquisitionManifest,
        destination: str | Path,
    ) -> str | None:
        """Write a caller-selected post-commit copy; return a safe warning on failure."""
        target = Path(destination)
        if target.exists() and (target.is_dir() or target.is_symlink()):
            return "optional manifest destination is not a regular file"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
            with temporary.open("xb") as stream:
                stream.write(_json_bytes(manifest.to_dict()))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            fsync_directory(target.parent)
        except OSError:
            return "optional manifest copy failed"
        return None

    def _path(self, acquisition_id: str) -> Path:
        digest = hashlib.sha256(acquisition_id.encode()).hexdigest()
        return self._root / f"v{CONTRACT_VERSION}" / digest[:2] / f"{acquisition_id}.json"

    @staticmethod
    def _validated_id(value: object) -> str:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise ManifestError("acquisition identifier uses an unsafe path grammar")
        return value

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ManifestError("clock must return a datetime")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return f"{serialized}\n".encode()
    except (TypeError, ValueError) as error:
        raise ArtifactError("manifest is not deterministic JSON") from error
