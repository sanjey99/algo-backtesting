"""Deterministic artifact publication boundary."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TextIO, cast

import pandas as pd

from src.analytics.sql_contracts import ArtifactInfo, ComparisonMetadata


class ArtifactExistsError(FileExistsError):
    """Raised when an artifact destination exists without explicit force mode."""


def serialize_supported_type(value: object) -> object:
    """Convert supported contract values to deterministic JSON primitives."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def write_comparison_bundle(
    frame: pd.DataFrame,
    metadata: ComparisonMetadata,
    csv_path: Path,
    metadata_path: Path,
    force: bool,
) -> tuple[ArtifactInfo, ArtifactInfo]:
    """Publish a CSV and its metadata as one atomic bundle."""
    destinations = (csv_path, metadata_path)
    _validate_distinct_destinations(destinations)
    _prepare_destinations(destinations, force)
    staged: tuple[Path, Path] | None = None
    try:
        csv_file = _temporary_text_file(csv_path)
        try:
            metadata_file = _temporary_text_file(metadata_path)
        except BaseException:
            csv_file.close()
            Path(csv_file.name).unlink(missing_ok=True)
            raise
        try:
            frame.to_csv(
                csv_file,
                index=False,
                encoding="utf-8",
                lineterminator="\n",
                na_rep="",
            )
            _dump_json(metadata, metadata_file)
            csv_file.flush()
            metadata_file.flush()
        finally:
            csv_file.close()
            metadata_file.close()
        staged = (Path(csv_file.name), Path(metadata_file.name))
        _publish_staged(staged, destinations, force)
    except BaseException:
        if staged is not None:
            _remove_paths(staged)
        else:
            staged_names = tuple(
                path
                for path in (
                    Path(csv_file.name) if "csv_file" in locals() else None,
                    Path(metadata_file.name) if "metadata_file" in locals() else None,
                )
                if path is not None
            )
            _remove_paths(staged_names)
        raise
    return (_artifact_info(csv_path), _artifact_info(metadata_path))


def write_json_artifact(value: object, path: Path, force: bool) -> ArtifactInfo:
    """Publish one deterministic versioned JSON artifact atomically."""
    _prepare_destinations((path,), force)
    temporary = _temporary_text_file(path)
    staged = Path(temporary.name)
    try:
        try:
            _dump_json(value, temporary)
            temporary.flush()
        finally:
            temporary.close()
        _publish_staged((staged,), (path,), force)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return _artifact_info(path)


def _dump_json(value: object, destination: TextIO) -> None:
    json.dump(
        value,
        destination,
        sort_keys=True,
        indent=2,
        default=serialize_supported_type,
        ensure_ascii=False,
    )
    destination.write("\n")


def _temporary_text_file(destination: Path) -> TextIO:
    return cast(
        TextIO,
        tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ),
    )


def _validate_distinct_destinations(destinations: tuple[Path, ...]) -> None:
    resolved = tuple(path.resolve(strict=False) for path in destinations)
    if len(set(resolved)) != len(resolved):
        raise ValueError("Artifact destinations must be distinct")


def _prepare_destinations(destinations: tuple[Path, ...], force: bool) -> None:
    for destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
    if force:
        return
    existing = tuple(destination for destination in destinations if destination.exists())
    if existing:
        names = ", ".join(str(path) for path in existing)
        raise ArtifactExistsError(f"Artifact destination already exists: {names}")


def _publish_staged(
    staged_paths: tuple[Path, ...], destinations: tuple[Path, ...], force: bool
) -> None:
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        if force:
            for destination in destinations:
                if destination.exists():
                    backup = _unused_backup_path(destination)
                    destination.replace(backup)
                    backups[destination] = backup
        for staged, destination in zip(staged_paths, destinations, strict=True):
            staged.replace(destination)
            published.append(destination)
    except BaseException:
        _remove_paths(tuple(reversed(published)))
        for destination, backup in backups.items():
            if backup.exists():
                backup.replace(destination)
        raise
    else:
        _remove_paths(tuple(backups.values()))


def _unused_backup_path(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".bak",
    )
    try:
        os.close(descriptor)
    except OSError:
        pass
    Path(raw_path).unlink()
    return Path(raw_path)


def _remove_paths(paths: tuple[Path, ...]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _artifact_info(path: Path) -> ArtifactInfo:
    content = path.read_bytes()
    return ArtifactInfo(path=path, byte_count=len(content), sha256=sha256(content).hexdigest())
