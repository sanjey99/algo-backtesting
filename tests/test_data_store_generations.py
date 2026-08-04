from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest

from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    ActionCoverage,
    ArtifactError,
    CachePublicationError,
    CacheStatus,
    ConcurrentPublicationError,
    LineageSegment,
    Provider,
)
from src.data.manifest import ManifestRepository
from src.data.store import DataStore


def _request(symbol: str = " aapl ") -> AcquisitionRequest:
    return AcquisitionRequest(symbol, date(2024, 1, 2), date(2024, 1, 3))


def _frame(symbol: str = "AAPL") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.Series(
                pd.to_datetime(["2024-01-02", "2024-01-03"]).astype("datetime64[ns]")
            ),
            "symbol": pd.Series([symbol, symbol], dtype="string"),
            "open": pd.Series([10.0, 11.0], dtype="float64"),
            "high": pd.Series([11.0, 12.0], dtype="float64"),
            "low": pd.Series([9.0, 10.0], dtype="float64"),
            "close": pd.Series([10.5, 11.5], dtype="float64"),
            "volume": pd.Series([100.0, 110.0], dtype="float64"),
            "adj_close": pd.Series([10.5, 11.5], dtype="float64"),
            "dividend_amount": pd.Series([0.0, 0.0], dtype="float64"),
            "split_coefficient": pd.Series([1.0, 1.0], dtype="float64"),
            "source": pd.Series(["yfinance", "yfinance"], dtype="string"),
        }
    )


def _manifest(
    request: AcquisitionRequest,
    acquisition_id: str = "request-1",
    lineage: tuple[LineageSegment, ...] = (),
) -> AcquisitionManifest:
    return AcquisitionManifest(
        acquisition_id,
        request,
        AcquisitionStatus.SUCCESS,
        started_at=datetime(2024, 1, 3, tzinfo=UTC),
        completed_at=datetime(2024, 1, 3, 0, 0, 1, tzinfo=UTC),
        lineage=lineage,
    )


def _lineage(day: date, content_hash: str) -> LineageSegment:
    return LineageSegment(
        start=day,
        end=day,
        provider=Provider.YFINANCE,
        acquired_at=datetime(2024, 1, 3, tzinfo=UTC),
        action_coverage=ActionCoverage.REPRESENTED,
        content_hash=content_hash,
        action_signature="no-corporate-actions",
    )


def test_namespace_and_empty_read_are_safe_and_noncreating(tmp_path: Path) -> None:
    store = DataStore(tmp_path, calendar_versions={"calendar_version": "test-1"})
    request = _request()

    namespace = store.generation_namespace(request)
    result = store.read_generation(request)

    assert namespace == tmp_path / "1" / "XNYS" / "1d" / "AAPL"
    assert result.status is CacheStatus.MISS
    assert result.frame is None
    assert not namespace.exists()


def test_publish_writes_hashed_generation_and_pointer_then_reads(tmp_path: Path) -> None:
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
        clock=lambda: datetime(2024, 1, 3, tzinfo=UTC),
    )
    request = _request()

    published = store.publish_generation(request, _frame(), {}, _manifest(request))
    result = store.read_generation(request)

    pointer = json.loads((store.generation_namespace(request) / "CURRENT.json").read_text())
    assert set(pointer["artifacts"]) == {
        "bars.parquet",
        "cache-metadata.json",
        "acquisition-manifest.json",
    }
    assert published.generation_id == "generation-1"
    assert result.status is CacheStatus.FULL_HIT
    assert result.generation_id == "generation-1"
    assert result.frame is not None
    pd.testing.assert_frame_equal(result.frame, _frame())


def test_reader_fails_closed_for_pointer_hash_and_contract_corruption(tmp_path: Path) -> None:
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request))
    namespace = store.generation_namespace(request)
    pointer_path = namespace / "CURRENT.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["artifacts"]["bars.parquet"]["sha256"] = "0" * 64
    pointer_path.write_text(json.dumps(pointer))

    result = store.read_generation(request)

    assert result.status is CacheStatus.INVALIDATED
    assert result.frame is None


def test_reader_treats_a_broken_current_pointer_link_as_invalidation(tmp_path: Path) -> None:
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request))
    pointer = store.generation_namespace(request) / "CURRENT.json"
    pointer.unlink()
    pointer.symlink_to("missing-CURRENT.json")

    result = store.read_generation(request)

    assert result.status is CacheStatus.INVALIDATED
    assert result.frame is None


def test_unreferenced_generation_is_never_read(tmp_path: Path) -> None:
    store = DataStore(tmp_path, calendar_versions={"calendar_version": "test-1"})
    request = _request()
    partial = store.generation_namespace(request) / "generations" / "orphan"
    partial.mkdir(parents=True)
    _frame().to_parquet(partial / "bars.parquet", index=False)

    result = store.read_generation(request)

    assert result.status is CacheStatus.MISS
    assert result.frame is None


def test_reader_rejects_a_symbolic_generation_container(tmp_path: Path) -> None:
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request))
    namespace = store.generation_namespace(request)
    generations = namespace / "generations"
    external_generations = tmp_path / "external-generations"
    generations.rename(external_generations)
    generations.symlink_to(external_generations, target_is_directory=True)

    result = store.read_generation(request)

    assert result.status is CacheStatus.INVALIDATED
    assert result.frame is None


def test_failure_before_pointer_replace_preserves_previous_generation(tmp_path: Path) -> None:
    request = _request()
    first = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
    )
    first.publish_generation(request, _frame(), {}, _manifest(request))

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated interruption")

    interrupted = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-2",
        replace_file=fail_replace,
    )
    with pytest.raises(CachePublicationError, match="publication failed"):
        interrupted.publish_generation(request, _frame(), {}, _manifest(request, "request-2"))

    recovered = first.read_generation(request)
    assert recovered.generation_id == "generation-1"
    assert recovered.status is CacheStatus.FULL_HIT


def test_stale_writer_rebases_and_preserves_nonconflicting_ranges(tmp_path: Path) -> None:
    identifiers: Iterator[str] = iter(("generation-1", "generation-2"))
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
    )
    request = _request()
    stale_base = store.current_generation_id(request)
    store.publish_generation(request, _frame().iloc[:1].copy(), {}, _manifest(request))

    store.publish_generation(
        request,
        _frame().iloc[1:].copy(),
        {},
        _manifest(request, "request-2"),
        base_generation_id=stale_base,
        revalidate=lambda candidate: len(candidate) == 2,
    )

    result = store.read_generation(request)
    assert result.generation_id == "generation-2"
    assert result.frame is not None
    pd.testing.assert_frame_equal(result.frame, _frame())


def test_stale_rebase_preserves_range_level_lineage_from_both_generations(
    tmp_path: Path,
) -> None:
    identifiers: Iterator[str] = iter(("generation-1", "generation-2"))
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
    )
    request = _request()
    stale_base = store.current_generation_id(request)
    first_segment = _lineage(date(2024, 1, 2), "a" * 64)
    second_segment = _lineage(date(2024, 1, 3), "b" * 64)
    store.publish_generation(
        request,
        _frame().iloc[:1].copy(),
        {},
        _manifest(request, lineage=(first_segment,)),
    )

    store.publish_generation(
        request,
        _frame().iloc[1:].copy(),
        {},
        _manifest(request, "request-2", lineage=(second_segment,)),
        base_generation_id=stale_base,
    )

    result = store.read_generation(request)
    assert result.manifest is not None
    assert result.manifest["lineage"] == _manifest(
        request,
        lineage=(first_segment, second_segment),
    ).to_dict()["lineage"]


def test_cleanup_retains_pointer_predecessor_not_newer_unreferenced_generation(
    tmp_path: Path,
) -> None:
    identifiers: Iterator[str] = iter(("generation-1", "generation-2", "generation-3"))
    times: Iterator[datetime] = iter(
        (
            datetime(2024, 1, 3, tzinfo=UTC),
            datetime(2024, 1, 4, tzinfo=UTC),
            datetime(2024, 1, 5, tzinfo=UTC),
        )
    )
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
        clock=lambda: next(times),
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-1"))
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-2"))
    pointer_path = store.generation_namespace(request) / "CURRENT.json"
    active_pointer = pointer_path.read_bytes()
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-3"))
    pointer_path.write_bytes(active_pointer)

    cleanup = store.cleanup_generations(request)

    generations = store.generation_namespace(request) / "generations"
    assert cleanup.removed_generation_ids == ("generation-3",)
    assert (generations / "generation-1").is_dir()
    assert (generations / "generation-2").is_dir()


def test_three_repeated_conflicts_fail_without_publication(tmp_path: Path) -> None:
    identifiers: Iterator[str] = iter(("generation-1", "generation-2", "generation-3"))
    request = _request()
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
    )
    store.publish_generation(request, _frame(), {}, _manifest(request))
    pointer_path = store.generation_namespace(request) / "CURRENT.json"
    pointer_one = pointer_path.read_bytes()
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-2"))
    pointer_two = pointer_path.read_bytes()

    def conflict(attempt: int, _generation_id: str | None) -> None:
        pointer_path.write_bytes(pointer_one if attempt % 2 else pointer_two)

    conflicting = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
        conflict_probe=conflict,
    )
    with pytest.raises(ConcurrentPublicationError, match="three times"):
        conflicting.publish_generation(
            request,
            _frame(),
            {},
            _manifest(request, "request-3"),
            base_generation_id="generation-2",
        )

    assert not (
        store.generation_namespace(request) / "generations" / "generation-3"
    ).exists()


def test_calendar_version_mismatch_invalidates_even_with_updated_hash(tmp_path: Path) -> None:
    store = DataStore(
        tmp_path,
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request))
    namespace = store.generation_namespace(request)
    pointer_path = namespace / "CURRENT.json"
    pointer = json.loads(pointer_path.read_text())
    metadata_path = namespace / pointer["artifacts"]["cache-metadata.json"]["path"]
    metadata = json.loads(metadata_path.read_text())
    metadata["calendar_versions"] = {"calendar_version": "other"}
    metadata_path.write_text(json.dumps(metadata))
    pointer["artifacts"]["cache-metadata.json"]["sha256"] = hashlib.sha256(
        metadata_path.read_bytes()
    ).hexdigest()
    pointer_path.write_text(json.dumps(pointer))

    assert store.read_generation(request).status is CacheStatus.INVALIDATED


class _SelectiveFailureRepository(ManifestRepository):
    def __init__(self, root: Path, failed_ids: set[str]) -> None:
        super().__init__(root)
        self.failed_ids = failed_ids

    def archive(self, manifest: AcquisitionManifest) -> Path:
        if manifest.acquisition_id in self.failed_ids:
            raise ArtifactError("injected archive failure")
        return super().archive(manifest)


def test_archive_failure_pins_falls_back_and_maintenance_unpins(tmp_path: Path) -> None:
    repository = _SelectiveFailureRepository(tmp_path / "reports", {"request-1"})
    identifiers: Iterator[str] = iter(("generation-1", "generation-2", "generation-3"))
    store = DataStore(
        tmp_path / "cache",
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: next(identifiers),
        manifest_repository=repository,
    )
    request = _request()
    first = store.publish_generation(request, _frame(), {}, _manifest(request, "request-1"))
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-2"))
    store.publish_generation(request, _frame(), {}, _manifest(request, "request-3"))

    assert first.warnings == ("cache committed but request report archival failed",)
    assert repository.lookup("request-1") is None
    assert store.lookup_manifest("request-1") == first.manifest.to_dict()
    cleanup = store.cleanup_generations(request)
    assert "generation-1" not in cleanup.removed_generation_ids

    repository.failed_ids.clear()
    assert store.maintain_manifest_archive() == ()
    assert repository.lookup("request-1") == first.manifest.to_dict()
    assert not (store.generation_namespace(request) / "pins" / "generation-1.json").exists()


def test_pinned_manifest_lookup_rejects_a_tampered_redirect_path(tmp_path: Path) -> None:
    repository = _SelectiveFailureRepository(tmp_path / "reports", {"request-1"})
    store = DataStore(
        tmp_path / "cache",
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
        manifest_repository=repository,
    )
    request = _request()
    publication = store.publish_generation(request, _frame(), {}, _manifest(request))
    namespace = store.generation_namespace(request)
    pin_path = namespace / "pins" / "generation-1.json"
    redirected_manifest = tmp_path / "redirected-manifest.json"
    redirected_manifest.write_text(json.dumps(publication.manifest.to_dict()))
    pin = json.loads(pin_path.read_text())
    pin["manifest_path"] = str(redirected_manifest)
    pin_path.write_text(json.dumps(pin))

    assert store.lookup_manifest("request-1") is None


@pytest.mark.parametrize("identity", ("acquisition", "generation"))
def test_manifest_maintenance_keeps_pin_for_embedded_identity_mismatch(
    tmp_path: Path,
    identity: str,
) -> None:
    repository = _SelectiveFailureRepository(tmp_path / "reports", {"request-1"})
    store = DataStore(
        tmp_path / "cache",
        calendar_versions={"calendar_version": "test-1"},
        generation_id_factory=lambda: "generation-1",
        manifest_repository=repository,
    )
    request = _request()
    store.publish_generation(request, _frame(), {}, _manifest(request))
    namespace = store.generation_namespace(request)
    embedded_manifest = namespace / "generations" / "generation-1" / "acquisition-manifest.json"
    document = json.loads(embedded_manifest.read_text())
    if identity == "acquisition":
        document["acquisition_id"] = "different-request"
    else:
        document["cache"]["generation_id"] = "different-generation"
    embedded_manifest.write_text(json.dumps(document))
    repository.failed_ids.clear()

    warnings = store.maintain_manifest_archive()

    assert warnings == ("manifest archival retry failed: ValueError",)
    assert repository.lookup("request-1") is None
    assert (namespace / "pins" / "generation-1.json").is_file()
