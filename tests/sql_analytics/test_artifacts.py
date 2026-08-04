"""Deterministic and atomic artifact publication tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from src.analytics.sql_artifacts import ArtifactExistsError, write_comparison_bundle
from src.analytics.sql_contracts import ComparisonMetadata, QueryId


def _metadata(
    *, row_count: int = 2, columns: tuple[str, ...] = ("name", "value")
) -> ComparisonMetadata:
    return ComparisonMetadata(
        schema_version="1.0",
        generated_at=datetime(2026, 8, 4, 12, 30, tzinfo=UTC),
        query_id=QueryId.STRATEGY_RUN_COMPARISON,
        sql_sha256="a" * 64,
        bound_params={
            "symbol": "SPY",
            "start_date": datetime(2024, 1, 1),
            "end_date": datetime(2024, 1, 31),
            "strategy_name": None,
        },
        database_identifier="/safe/demo.db",
        row_count=row_count,
        ordered_columns=columns,
        contract_version="1.0",
        contract_valid=True,
        validation_report_path="",
        diagnostic_override=False,
    )


def test_comparison_metadata_snapshots_bound_parameters_immutably() -> None:
    """Mutating a caller-owned dictionary cannot rewrite already-recorded query provenance."""
    bound_params = {"symbol": "SPY"}
    metadata = ComparisonMetadata(
        schema_version="1.0",
        generated_at=datetime(2026, 8, 4, 12, 30, tzinfo=UTC),
        query_id=QueryId.STRATEGY_RUN_COMPARISON,
        sql_sha256="a" * 64,
        bound_params=bound_params,
        database_identifier="/safe/demo.db",
        row_count=0,
        ordered_columns=(),
        contract_version="1.0",
        contract_valid=True,
        validation_report_path="",
        diagnostic_override=False,
    )

    bound_params["symbol"] = "QQQ"

    assert metadata.bound_params["symbol"] == "SPY"
    with pytest.raises(TypeError):
        metadata.bound_params["symbol"] = "IWM"  # type: ignore[index]


def test_bundle_has_stable_utf8_csv_and_versioned_sorted_json(tmp_path: Path) -> None:
    """Changing CSV encoding/options or JSON ordering must change this observable byte contract."""
    frame = pd.DataFrame({"name": ["café", "plain"], "value": [1.5, None]})
    csv_path = tmp_path / "nested" / "comparison.csv"
    metadata_path = tmp_path / "nested" / "comparison.json"

    csv_info, metadata_info = write_comparison_bundle(
        frame, _metadata(), csv_path, metadata_path, force=False
    )

    expected_csv = "name,value\ncafé,1.5\nplain,\n".encode()
    assert csv_path.read_bytes() == expected_csv
    assert not csv_path.read_bytes().startswith(b",")
    assert b"\r\n" not in csv_path.read_bytes()
    assert csv_info.path == csv_path
    assert csv_info.byte_count == len(expected_csv)
    assert len(csv_info.sha256) == 64

    metadata_bytes = metadata_path.read_bytes()
    assert metadata_bytes.endswith(b"\n")
    assert metadata_bytes == (
        b'{\n'
        b'  "bound_params": {\n'
        b'    "end_date": "2024-01-31T00:00:00",\n'
        b'    "start_date": "2024-01-01T00:00:00",\n'
        b'    "strategy_name": null,\n'
        b'    "symbol": "SPY"\n'
        b'  },\n'
        b'  "contract_valid": true,\n'
        b'  "contract_version": "1.0",\n'
        b'  "database_identifier": "/safe/demo.db",\n'
        b'  "diagnostic_override": false,\n'
        b'  "generated_at": "2026-08-04T12:30:00+00:00",\n'
        b'  "ordered_columns": [\n'
        b'    "name",\n'
        b'    "value"\n'
        b'  ],\n'
        b'  "query_id": "strategy_run_comparison",\n'
        b'  "row_count": 2,\n'
        b'  "schema_version": "1.0",\n'
        b'  "sql_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",\n'
        b'  "validation_report_path": ""\n'
        b'}\n'
    )
    assert metadata_info.path == metadata_path
    assert metadata_info.byte_count == len(metadata_bytes)
    assert len(metadata_info.sha256) == 64


def test_preflight_refuses_either_existing_destination_without_writing(tmp_path: Path) -> None:
    """Removing either preflight check could leave a new half-pair beside an old artifact."""
    frame = pd.DataFrame({"name": ["new"], "value": [1]})
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    metadata_path.write_text("old metadata", encoding="utf-8")

    with pytest.raises(ArtifactExistsError, match="comparison.json"):
        write_comparison_bundle(
            frame,
            _metadata(row_count=1),
            csv_path,
            metadata_path,
            force=False,
        )

    assert not csv_path.exists()
    assert metadata_path.read_text(encoding="utf-8") == "old metadata"


def test_force_replaces_both_existing_artifacts(tmp_path: Path) -> None:
    """Dropping force-mode backup/publication would preserve stale output or only one new file."""
    frame = pd.DataFrame({"name": ["new"], "value": [1]})
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    csv_path.write_text("old csv", encoding="utf-8")
    metadata_path.write_text("old metadata", encoding="utf-8")

    write_comparison_bundle(
        frame,
        _metadata(row_count=1),
        csv_path,
        metadata_path,
        force=True,
    )

    assert csv_path.read_text(encoding="utf-8") == "name,value\nnew,1\n"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["row_count"] == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "comparison.csv",
        "comparison.json",
    ]


def test_serialization_failure_cleans_sibling_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serializer exception must not leak staged files or publish either destination."""
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"

    def fail_to_csv(self: pd.DataFrame, path: object, **kwargs: object) -> None:
        staged_name = getattr(path, "name")
        Path(str(staged_name)).write_text("partial", encoding="utf-8")
        raise UnicodeError("controlled serialization failure")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail_to_csv)

    with pytest.raises(UnicodeError, match="controlled"):
        write_comparison_bundle(
            pd.DataFrame({"name": ["new"], "value": [1]}),
            _metadata(row_count=1),
            csv_path,
            metadata_path,
            force=False,
        )

    assert tuple(tmp_path.iterdir()) == ()


def test_second_publish_failure_removes_new_half_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing CSV before metadata must roll CSV back when the second replace fails."""
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    original_replace = Path.replace
    failed = False

    def fail_second_publish(self: Path, target: Path) -> Path:
        nonlocal failed
        if Path(target) == metadata_path and not failed:
            failed = True
            raise OSError("controlled second publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_second_publish)

    with pytest.raises(OSError, match="controlled second publish"):
        write_comparison_bundle(
            pd.DataFrame({"name": ["new"], "value": [1]}),
            _metadata(row_count=1),
            csv_path,
            metadata_path,
            force=False,
        )

    assert tuple(tmp_path.iterdir()) == ()


def test_second_force_publish_failure_restores_both_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure after backing up old output must restore both exact original byte streams."""
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    csv_path.write_bytes(b"original csv\n")
    metadata_path.write_bytes(b"original metadata\n")
    original_replace = Path.replace
    failed = False

    def fail_second_publish(self: Path, target: Path) -> Path:
        nonlocal failed
        if Path(target) == metadata_path and not failed:
            failed = True
            raise OSError("controlled second publish failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_second_publish)

    with pytest.raises(OSError, match="controlled second publish"):
        write_comparison_bundle(
            pd.DataFrame({"name": ["new"], "value": [1]}),
            _metadata(row_count=1),
            csv_path,
            metadata_path,
            force=True,
        )

    assert csv_path.read_bytes() == b"original csv\n"
    assert metadata_path.read_bytes() == b"original metadata\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "comparison.csv",
        "comparison.json",
    ]
