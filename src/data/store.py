"""Legacy exact-range cache and immutable canonical cache generations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pandas as pd
from filelock import FileLock

from src.data.calendars import get_market_calendar
from src.data.contracts import (
    CONTRACT_VERSION,
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    CachePublicationError,
    CacheStatus,
    ConcurrentPublicationError,
    ContractViolationError,
    json_safe,
)
from src.data.fetcher import DataFetcher
from src.data.manifest import ManifestRepository
from src.data.store_artifacts import _merge_lineage, _rebase_lineage, _validate_canonical

_ARTIFACT_NAMES = (
    "bars.parquet",
    "cache-metadata.json",
    "acquisition-manifest.json",
)
_SAFE_GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_LEGACY_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_POINTER_SCHEMA_VERSION = "1"


class _LockFactory(Protocol):
    def __call__(self, path: Path) -> AbstractContextManager[Any]: ...


@dataclass(frozen=True, slots=True)
class CacheReadResult:
    """Fail-closed result returned by a canonical generation read."""

    status: CacheStatus
    frame: pd.DataFrame | None = None
    metadata: Mapping[str, Any] | None = None
    manifest: Mapping[str, Any] | None = None
    generation_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationPublication:
    generation_id: str
    frame: pd.DataFrame
    metadata: Mapping[str, Any]
    manifest: AcquisitionManifest
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupResult:
    removed_generation_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()


class DataStore:
    """Parquet cache with an immutable generation API and legacy compatibility."""

    def __init__(
        self,
        cache_dir: str | Path = "data/raw",
        *,
        calendar_versions: Mapping[str, str] | None = None,
        generation_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        lock_factory: _LockFactory | None = None,
        replace_file: Callable[[Path, Path], None] | None = None,
        manifest_repository: ManifestRepository | None = None,
        conflict_probe: Callable[[int, str | None], None] | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        evidence = calendar_versions or get_market_calendar().version_evidence()
        self._calendar_versions = _plain_string_mapping(evidence, "calendar versions")
        self._generation_id_factory = generation_id_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock_factory = lock_factory or (lambda path: FileLock(str(path)))
        self._replace_file = replace_file or os.replace
        self._manifest_repository = manifest_repository
        self._conflict_probe = conflict_probe

    @property
    def archives_publications(self) -> bool:
        """Whether successful publications are archived as part of store commit handling."""
        return self._manifest_repository is not None

    # Legacy exact-range API retained until the route migration is complete.
    def _cache_path(self, symbol: str, start: datetime, end: datetime) -> Path:
        if not isinstance(symbol, str) or not _SAFE_LEGACY_SYMBOL.fullmatch(symbol):
            raise ContractViolationError("legacy cache symbol uses an unsafe path grammar")
        if not isinstance(start, datetime) or not isinstance(end, datetime) or start > end:
            raise ContractViolationError("legacy cache range is invalid")
        fname = f"{symbol}_{start.date()}_{end.date()}.parquet"
        return self._cache_dir / fname

    def get(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame | None:
        path = self._cache_path(symbol, start, end)
        if not path.exists():
            return None
        frame: pd.DataFrame = pd.read_parquet(path)
        return frame

    def save(self, symbol: str, start: datetime, end: datetime, df: pd.DataFrame) -> None:
        self._cache_path(symbol, start, end).parent.mkdir(parents=True, exist_ok=True)
        df.copy(deep=True).to_parquet(self._cache_path(symbol, start, end))

    def fetch_or_cache(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        fetcher: DataFetcher,
    ) -> pd.DataFrame:
        cached = self.get(symbol, start, end)
        if cached is not None:
            return cached
        frame = fetcher.fetch(symbol, start, end)
        self.save(symbol, start, end, frame)
        return frame

    # Canonical generation API.
    def generation_namespace(self, request: AcquisitionRequest) -> Path:
        """Return the validated contract/calendar/interval/symbol namespace."""
        if not isinstance(request, AcquisitionRequest):
            raise ContractViolationError("cache request must be syntactically admitted")
        return (
            self._cache_dir
            / CONTRACT_VERSION
            / request.calendar
            / request.interval
            / request.symbol
        )

    def current_generation_id(self, request: AcquisitionRequest) -> str | None:
        """Return only a schema-valid pointer identifier, never scan generations."""
        pointer = self._load_pointer(self.generation_namespace(request))
        if pointer is None:
            return None
        return cast(str, pointer["generation_id"])

    def read_generation(self, request: AcquisitionRequest) -> CacheReadResult:
        """Follow CURRENT and verify all artifacts, hashes, and canonical contracts."""
        namespace = self.generation_namespace(request)
        pointer_path = namespace / "CURRENT.json"
        if pointer_path.is_symlink():
            return CacheReadResult(CacheStatus.INVALIDATED, reason="CURRENT pointer is symbolic")
        if not pointer_path.exists():
            return CacheReadResult(CacheStatus.MISS)
        try:
            pointer = self._load_pointer(namespace, required=True)
            assert pointer is not None
            return self._read_referenced(request, namespace, pointer)
        except Exception as error:
            return CacheReadResult(CacheStatus.INVALIDATED, reason=_safe_reason(error))

    def publish_generation(
        self,
        request: AcquisitionRequest,
        frame: pd.DataFrame,
        metadata: Mapping[str, Any],
        manifest: AcquisitionManifest,
        *,
        base_generation_id: str | None = None,
        revalidate: Callable[[pd.DataFrame], bool | pd.DataFrame] | None = None,
        replace_ranges: tuple[tuple[date, date], ...] = (),
    ) -> GenerationPublication:
        """Compare/rebase/validate/publish under a bounded cross-process lock."""
        if manifest.request != request:
            raise ContractViolationError("manifest request does not match cache namespace")
        if manifest.started_at is None or manifest.completed_at is None:
            raise ContractViolationError("publication manifest requires UTC acquisition times")
        if manifest.status not in {AcquisitionStatus.SUCCESS, AcquisitionStatus.PARTIAL_SUCCESS}:
            raise ContractViolationError("only successful acquisitions may publish cache data")
        candidate = frame.copy(deep=True)
        _validate_canonical(candidate, request)
        supplied_metadata = _plain_json_mapping(metadata, "cache metadata")
        namespace = self.generation_namespace(request)
        expected_base = base_generation_id

        for conflict_number in range(1, 4):
            namespace.mkdir(parents=True, exist_ok=True)
            with self._lock_factory(namespace / ".publish.lock"):
                latest = self.read_generation(request)
                if latest.status is CacheStatus.INVALIDATED:
                    raise CachePublicationError("current cache generation is invalid")
                latest_id = latest.generation_id
                assembled = candidate.copy(deep=True)
                publication_manifest = manifest
                if expected_base != latest_id and latest.frame is not None:
                    incoming = (
                        _rows_in_ranges(assembled, replace_ranges) if replace_ranges else assembled
                    )
                    assembled = _merge_canonical(latest.frame, incoming)
                    if latest.manifest is None:
                        raise CachePublicationError("current cache generation has no manifest")
                    publication_manifest = replace(
                        manifest,
                        lineage=(
                            _rebase_lineage(
                                latest.manifest,
                                manifest.lineage,
                                assembled,
                                replace_ranges,
                            )
                            if replace_ranges
                            else _merge_lineage(latest.manifest, manifest.lineage)
                        ),
                    )
                _validate_canonical(assembled, request)
                if revalidate is not None:
                    checked = revalidate(assembled.copy(deep=True))
                    if isinstance(checked, pd.DataFrame):
                        assembled = checked.copy(deep=True)
                        _validate_canonical(assembled, request)
                    elif checked is not True:
                        raise ContractViolationError("rebased cache failed complete validation")

                if self._conflict_probe is not None:
                    self._conflict_probe(conflict_number, latest_id)
                observed = self.current_generation_id(request)
                if observed != latest_id:
                    expected_base = latest_id
                    continue
                publication = self._publish_locked(
                    request,
                    assembled,
                    supplied_metadata,
                    publication_manifest,
                    namespace,
                    previous_generation_id=latest_id,
                )
            return self._archive_after_commit(request, publication)
        raise ConcurrentPublicationError("cache publication conflicted three times")

    def cleanup_generations(self, request: AcquisitionRequest) -> CleanupResult:
        """Retain active, immediately prior valid, and all pinned generations."""
        namespace = self.generation_namespace(request)
        generations = namespace / "generations"
        if not generations.exists():
            return CleanupResult(())
        current = self.read_generation(request)
        if current.status not in {CacheStatus.FULL_HIT, CacheStatus.PARTIAL_HIT}:
            return CleanupResult((), ("generation cleanup skipped because CURRENT is invalid",))
        active = current.generation_id
        pinned = self._pinned_generation_ids(namespace)
        retain = {item for item in (active,) if item is not None} | pinned
        pointer = self._load_pointer(namespace, required=True)
        assert pointer is not None
        previous_id = cast(str | None, pointer["previous_generation_id"])
        if previous_id is not None:
            previous = generations / previous_id
            try:
                previous_metadata = _load_json(previous / "cache-metadata.json")
                if self._generation_directory_valid(request, previous, previous_metadata):
                    retain.add(previous_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        removed: list[str] = []
        warnings: list[str] = []
        for path in generations.iterdir():
            if not path.is_dir() or path.name in retain:
                continue
            try:
                shutil.rmtree(path)
                removed.append(path.name)
            except OSError:
                warnings.append(f"cleanup failed for generation {path.name}")
        if removed:
            try:
                _fsync_directory(generations)
            except OSError:
                warnings.append("generation cleanup directory sync failed")
        return CleanupResult(tuple(sorted(removed)), tuple(warnings))

    def lookup_manifest(self, acquisition_id: str) -> dict[str, Any] | None:
        """Look up the archive, falling back only to a pinned embedded report."""
        if self._manifest_repository is not None:
            archived = self._manifest_repository.lookup(acquisition_id)
            if archived is not None:
                return archived
        for namespace, pin_path in self._pin_paths():
            try:
                generation_id, pinned_acquisition_id = self._load_pin(namespace, pin_path)
                if pinned_acquisition_id != acquisition_id:
                    continue
                document = self._pinned_embedded_manifest(
                    namespace,
                    generation_id,
                    pinned_acquisition_id,
                )
                return cast(dict[str, Any], json.loads(json.dumps(document)))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return None

    def maintain_manifest_archive(self) -> tuple[str, ...]:
        """Retry pinned post-commit archives and unpin successful reports."""
        if self._manifest_repository is None:
            return ("manifest maintenance skipped because no repository is configured",)
        warnings: list[str] = []
        for namespace, pin_path in self._pin_paths():
            try:
                generation_id, acquisition_id = self._load_pin(namespace, pin_path)
                document = self._pinned_embedded_manifest(namespace, generation_id, acquisition_id)
                self._manifest_repository.archive_document(acquisition_id, document)
                pin_path.unlink()
                _fsync_directory(pin_path.parent)
            except Exception as error:
                # Artifact exceptions are intentionally secondary during maintenance.
                warnings.append(f"manifest archival retry failed: {type(error).__name__}")
        return tuple(warnings)

    def _publish_locked(
        self,
        request: AcquisitionRequest,
        frame: pd.DataFrame,
        metadata: Mapping[str, Any],
        manifest: AcquisitionManifest,
        namespace: Path,
        *,
        previous_generation_id: str | None,
    ) -> GenerationPublication:
        generation_id = _validate_generation_id(self._generation_id_factory())
        generation = namespace / "generations" / generation_id
        if generation.parent.exists() and generation.parent.is_symlink():
            raise CachePublicationError("generation container must not be a symbolic link")
        if generation.exists():
            raise CachePublicationError("generation identifier already exists")
        generation.mkdir(parents=True, exist_ok=False)
        created_at = self._utc_now().isoformat()
        try:
            bars_path = generation / "bars.parquet"
            frame.copy(deep=True).to_parquet(bars_path, index=False)
            _fsync_file(bars_path)
            bars_hash = _sha256_file(bars_path)
            cache_key = str(namespace.relative_to(self._cache_dir))
            final_manifest = replace(
                manifest,
                cache=replace(
                    manifest.cache,
                    generation_id=generation_id,
                    cache_key=cache_key,
                ),
                output_hash=bars_hash,
            )
            metadata_document: dict[str, Any] = {
                **metadata,
                "schema_version": _POINTER_SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "calendar": request.calendar,
                "calendar_versions": dict(self._calendar_versions),
                "interval": request.interval,
                "symbol": request.symbol,
                "generation_id": generation_id,
                "created_at": created_at,
            }
            metadata_path = generation / "cache-metadata.json"
            manifest_path = generation / "acquisition-manifest.json"
            _write_json(metadata_path, metadata_document)
            _write_json(manifest_path, final_manifest.to_dict())
            artifacts = {
                name: {
                    "path": f"generations/{generation_id}/{name}",
                    "sha256": _sha256_file(generation / name),
                }
                for name in _ARTIFACT_NAMES
            }
            _fsync_directory(generation)
            _fsync_directory(generation.parent)
            pointer = {
                "schema_version": _POINTER_SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "generation_id": generation_id,
                "previous_generation_id": previous_generation_id,
                "created_at": created_at,
                "artifacts": artifacts,
            }
            temporary_pointer = namespace / f".CURRENT.{uuid.uuid4().hex}.tmp"
            _write_json(temporary_pointer, pointer)
            self._replace_file(temporary_pointer, namespace / "CURRENT.json")
            _fsync_directory(namespace)
            return GenerationPublication(
                generation_id,
                frame.copy(deep=True),
                json.loads(json.dumps(metadata_document)),
                final_manifest,
            )
        except Exception as error:
            if isinstance(error, CachePublicationError):
                raise
            raise CachePublicationError("immutable cache generation publication failed") from error

    def _archive_after_commit(
        self,
        request: AcquisitionRequest,
        publication: GenerationPublication,
    ) -> GenerationPublication:
        if self._manifest_repository is None:
            return publication
        try:
            self._manifest_repository.archive(publication.manifest)
            return publication
        except Exception:
            warning = "cache committed but request report archival failed"
            namespace = self.generation_namespace(request)
            try:
                self._pin(namespace, publication.generation_id, publication.manifest.acquisition_id)
            except OSError:
                warning += "; generation pin publication failed"
            return replace(publication, warnings=(warning,))

    def _pin(self, namespace: Path, generation_id: str, acquisition_id: str) -> None:
        pins = namespace / "pins"
        pins.mkdir(parents=True, exist_ok=True)
        _write_json(
            pins / f"{generation_id}.json",
            {
                "schema_version": _POINTER_SCHEMA_VERSION,
                "generation_id": generation_id,
                "acquisition_id": acquisition_id,
            },
        )
        _fsync_directory(pins)

    def _pin_paths(self) -> Iterator[tuple[Path, Path]]:
        pattern = f"{CONTRACT_VERSION}/*/*/*/pins/*.json"
        for pin_path in self._cache_dir.glob(pattern):
            yield pin_path.parent.parent, pin_path

    def _load_pin(self, namespace: Path, pin_path: Path) -> tuple[str, str]:
        if pin_path.is_symlink() or pin_path.parent.is_symlink():
            raise ValueError("generation pin must not be symbolic")
        if pin_path.parent.name != "pins":
            raise ValueError("generation pin has an invalid namespace")
        self._validate_namespace_path(namespace)
        document = _load_json(pin_path)
        if set(document) != {"schema_version", "generation_id", "acquisition_id"}:
            raise ValueError("generation pin has an invalid schema")
        if document["schema_version"] != _POINTER_SCHEMA_VERSION:
            raise ValueError("generation pin schema version is incompatible")
        generation_id = _validate_generation_id(document["generation_id"])
        acquisition_id = _validate_acquisition_id(document["acquisition_id"])
        if pin_path.stem != generation_id:
            raise ValueError("generation pin identity does not match its filename")
        return generation_id, acquisition_id

    def _pinned_embedded_manifest(
        self,
        namespace: Path,
        generation_id: str,
        acquisition_id: str,
    ) -> dict[str, Any]:
        generation = self._referenced_generation_path(namespace, generation_id)
        manifest_path = generation / "acquisition-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("pinned embedded manifest is unavailable")
        document = _load_json(manifest_path)
        cache = document.get("cache")
        if (
            document.get("acquisition_id") != acquisition_id
            or not isinstance(cache, Mapping)
            or cache.get("generation_id") != generation_id
        ):
            raise ValueError("pinned embedded manifest identities are incompatible")
        return document

    def _pinned_generation_ids(self, namespace: Path) -> set[str]:
        pins = namespace / "pins"
        if not pins.exists():
            return set()
        return {
            path.stem for path in pins.glob("*.json") if _SAFE_GENERATION_ID.fullmatch(path.stem)
        }

    def _validate_namespace_path(self, namespace: Path) -> None:
        try:
            relative = namespace.relative_to(self._cache_dir)
        except ValueError as error:
            raise ValueError("cache namespace escapes the configured cache root") from error
        if len(relative.parts) != 4 or relative.parts[0] != CONTRACT_VERSION:
            raise ValueError("cache namespace has an invalid layout")
        current = self._cache_dir
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ValueError("cache namespace must not contain symbolic components")

    def _referenced_generation_path(self, namespace: Path, generation_id: str) -> Path:
        self._validate_namespace_path(namespace)
        generations = namespace / "generations"
        if generations.is_symlink() or not generations.is_dir():
            raise ValueError("generation container is missing or symbolic")
        generation = generations / generation_id
        if generation.is_symlink() or not generation.is_dir():
            raise ValueError("referenced generation is missing or symbolic")
        try:
            resolved_generation = generation.resolve(strict=True)
            resolved_namespace = namespace.resolve(strict=True)
            resolved_generation.relative_to(resolved_namespace)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("referenced generation escapes its cache namespace") from error
        return generation

    def _load_pointer(self, namespace: Path, *, required: bool = False) -> dict[str, Any] | None:
        path = namespace / "CURRENT.json"
        if not path.exists():
            if required:
                raise ValueError("CURRENT pointer is missing")
            return None
        if path.is_symlink():
            raise ValueError("CURRENT pointer must not be a symbolic link")
        pointer = _load_json(path)
        expected = {
            "schema_version",
            "contract_version",
            "generation_id",
            "previous_generation_id",
            "created_at",
            "artifacts",
        }
        if set(pointer) != expected:
            raise ValueError("CURRENT pointer has an invalid schema")
        if pointer["schema_version"] != _POINTER_SCHEMA_VERSION:
            raise ValueError("CURRENT pointer schema version is incompatible")
        if pointer["contract_version"] != CONTRACT_VERSION:
            raise ValueError("CURRENT contract version is incompatible")
        _parse_utc_timestamp(pointer["created_at"])
        generation_id = _validate_generation_id(pointer["generation_id"])
        previous = pointer["previous_generation_id"]
        if previous is not None:
            _validate_generation_id(previous)
        artifacts = pointer["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(_ARTIFACT_NAMES):
            raise ValueError("CURRENT artifact set is invalid")
        for name in _ARTIFACT_NAMES:
            evidence = artifacts[name]
            expected_path = f"generations/{generation_id}/{name}"
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"path", "sha256"}
                or evidence["path"] != expected_path
                or not _is_sha256(evidence["sha256"])
            ):
                raise ValueError("CURRENT artifact evidence is invalid")
        return pointer

    def _read_referenced(
        self,
        request: AcquisitionRequest,
        namespace: Path,
        pointer: Mapping[str, Any],
    ) -> CacheReadResult:
        generation_id = cast(str, pointer["generation_id"])
        generation = self._referenced_generation_path(namespace, generation_id)
        artifacts = cast(Mapping[str, Mapping[str, str]], pointer["artifacts"])
        for name in _ARTIFACT_NAMES:
            path = namespace / artifacts[name]["path"]
            if not path.is_file() or path.is_symlink():
                raise ValueError("referenced generation artifact is missing")
            if _sha256_file(path) != artifacts[name]["sha256"]:
                raise ValueError("referenced generation artifact hash mismatch")
        metadata = _load_json(generation / "cache-metadata.json")
        if not self._generation_directory_valid(request, generation, metadata):
            raise ValueError("cache metadata is incompatible")
        manifest = _load_json(generation / "acquisition-manifest.json")
        bars_hash = artifacts["bars.parquet"]["sha256"]
        cache_document = manifest.get("cache")
        if (
            manifest.get("schema_version") != CONTRACT_VERSION
            or manifest.get("status") not in {"success", "partial_success"}
            or not _manifest_namespace_compatible(manifest.get("request"), request)
            or not isinstance(cache_document, dict)
            or cache_document.get("generation_id") != generation_id
            or cache_document.get("cache_key") != str(namespace.relative_to(self._cache_dir))
            or manifest.get("output_hash") != bars_hash
        ):
            raise ValueError("embedded acquisition manifest is incompatible")
        _parse_utc_timestamp(manifest.get("started_at"))
        _parse_utc_timestamp(manifest.get("completed_at"))
        frame = pd.read_parquet(generation / "bars.parquet")
        _validate_canonical(frame, request)
        return CacheReadResult(
            CacheStatus.FULL_HIT,
            frame.copy(deep=True),
            json.loads(json.dumps(metadata)),
            json.loads(json.dumps(manifest)),
            generation_id,
        )

    def _generation_directory_valid(
        self,
        request: AcquisitionRequest,
        generation: Path,
        metadata: Mapping[str, Any],
    ) -> bool:
        if metadata.get("schema_version") != _POINTER_SCHEMA_VERSION:
            return False
        if metadata.get("contract_version") != CONTRACT_VERSION:
            return False
        if metadata.get("calendar") != request.calendar:
            return False
        if metadata.get("calendar_versions") != dict(self._calendar_versions):
            return False
        if metadata.get("interval") != request.interval or metadata.get("symbol") != request.symbol:
            return False
        if metadata.get("generation_id") != generation.name:
            return False
        try:
            _parse_utc_timestamp(metadata.get("created_at"))
            frame = pd.read_parquet(generation / "bars.parquet")
            _validate_canonical(frame, request)
            manifest = _load_json(generation / "acquisition-manifest.json")
            if (
                manifest.get("schema_version") != CONTRACT_VERSION
                or not _manifest_namespace_compatible(manifest.get("request"), request)
                or manifest.get("output_hash") != _sha256_file(generation / "bars.parquet")
            ):
                return False
        except (TypeError, ValueError):
            return False
        except Exception:
            return False
        return all((generation / name).is_file() for name in _ARTIFACT_NAMES)

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise CachePublicationError("clock must return a datetime")
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_generation_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_GENERATION_ID.fullmatch(value):
        raise ValueError("generation identifier uses an unsafe path grammar")
    return value


def _validate_acquisition_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_GENERATION_ID.fullmatch(value):
        raise ValueError("acquisition identifier uses an unsafe path grammar")
    return value


def _merge_canonical(current: pd.DataFrame, candidate: pd.DataFrame) -> pd.DataFrame:
    merged = pd.concat([current.copy(deep=True), candidate.copy(deep=True)], ignore_index=True)
    merged = merged.drop_duplicates("timestamp", keep="last")
    return merged.sort_values("timestamp", kind="mergesort").reset_index(drop=True)


def _rows_in_ranges(
    frame: pd.DataFrame,
    ranges: tuple[tuple[date, date], ...],
) -> pd.DataFrame:
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    selected = pd.Series(False, index=frame.index)
    for start, end in ranges:
        selected |= (timestamps >= pd.Timestamp(start)) & (timestamps <= pd.Timestamp(end))
    return frame.loc[selected].copy(deep=True).reset_index(drop=True)


def _manifest_namespace_compatible(value: object, request: AcquisitionRequest) -> bool:
    """Check only fields that form the symbol-level generation namespace."""
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("symbol") == request.symbol
        and value.get("interval") == request.interval
        and value.get("calendar") == request.calendar
    )


def _plain_string_mapping(value: Mapping[str, str], name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ContractViolationError(f"{name} must be a non-empty mapping")
    result = dict(value)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in result.items()):
        raise ContractViolationError(f"{name} must contain string keys and values")
    return result


def _plain_json_mapping(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractViolationError(f"{name} must be a mapping")
    safe = json_safe(value)
    if not isinstance(safe, dict):
        raise ContractViolationError(f"{name} must serialize to a JSON object")
    try:
        return cast(dict[str, Any], json.loads(json.dumps(safe)))
    except (TypeError, ValueError) as error:
        raise ContractViolationError(f"{name} must be JSON-safe") from error


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object")
    return cast(dict[str, Any], value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload = f"{serialized}\n".encode()
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_reason(error: BaseException) -> str:
    return str(error)[:200] or type(error).__name__


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp evidence must be an ISO string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("timestamp evidence must be UTC")
    return parsed
