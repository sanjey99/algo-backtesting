from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    AttemptEvidence,
    CacheEvidence,
    CacheStatus,
    InvalidRequestError,
    ManifestError,
    Provider,
)
from src.data.manifest import ManifestRepository


def _manifest(acquisition_id: str, secret: str = "known-secret") -> AcquisitionManifest:
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3))
    return AcquisitionManifest(
        acquisition_id,
        request,
        AcquisitionStatus.FAILED,
        started_at=datetime(2024, 1, 3, tzinfo=UTC),
        completed_at=datetime(2024, 1, 3, 0, 0, 1, tzinfo=UTC),
        attempts=(
            AttemptEvidence(
                Provider.ALPHA_VANTAGE,
                1,
                datetime(2024, 1, 3, tzinfo=UTC),
                1.0,
                "failed",
                error_message=f"https://example.test/query?apikey={secret}&page=1",
            ),
        ),
    )


def test_id_is_allocated_only_after_request_admission(tmp_path: Path) -> None:
    calls = 0

    def allocate() -> str:
        nonlocal calls
        calls += 1
        return "request-1"

    repository = ManifestRepository(tmp_path, id_factory=allocate)
    with pytest.raises(InvalidRequestError):
        repository.admit(symbol="../SPY", start=date(2024, 1, 2), end=date(2024, 1, 3))
    admission = repository.admit(symbol="spy", start=date(2024, 1, 2), end=date(2024, 1, 3))

    assert calls == 1
    assert admission.acquisition_id == "request-1"
    assert admission.request.symbol == "SPY"


def test_archive_is_deterministic_immutable_and_redacted(tmp_path: Path) -> None:
    repository = ManifestRepository(tmp_path)
    manifest = _manifest("request-1")

    path = repository.archive(manifest)
    first = path.read_bytes()
    assert repository.archive(manifest) == path
    assert path.read_bytes() == first
    assert repository.lookup("request-1") == manifest.to_dict()
    payload = first.decode()
    assert "known-secret" not in payload
    assert "apikey=" not in payload.lower()
    assert json.loads(payload)["schema_version"] == "1"
    assert "+00:00" in payload

    conflicting = _manifest("request-1", secret="different")
    conflicting = AcquisitionManifest(
        conflicting.acquisition_id,
        conflicting.request,
        AcquisitionStatus.SUCCESS,
        started_at=conflicting.started_at,
        completed_at=conflicting.completed_at,
    )
    with pytest.raises(ManifestError):
        repository.archive(conflicting)


@pytest.mark.parametrize(
    ("status", "cache_status"),
    [
        (AcquisitionStatus.SUCCESS, CacheStatus.MISS),
        (AcquisitionStatus.SUCCESS, CacheStatus.FULL_HIT),
        (AcquisitionStatus.FAILED, CacheStatus.MISS),
    ],
)
def test_archives_complete_admitted_reports_for_success_hits_and_failures(
    tmp_path: Path,
    status: AcquisitionStatus,
    cache_status: CacheStatus,
) -> None:
    repository = ManifestRepository(tmp_path)
    manifest = AcquisitionManifest(
        acquisition_id=f"request-{status.value}-{cache_status.value}",
        request=AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3)),
        status=status,
        cache=CacheEvidence(cache_status),
        counters={"accepted_rows": 2, "attempts": 1},
        started_at=datetime(2024, 1, 3, tzinfo=UTC),
        completed_at=datetime(2024, 1, 3, 0, 0, 1, tzinfo=UTC),
    )

    path = repository.archive(manifest)
    document = json.loads(path.read_text())

    assert document == manifest.to_dict()
    assert document["schema_version"] == "1"
    assert document["quality_policy"]["minimum_coverage"] == 0.98
    assert document["retry_policy"]["max_attempts"] == 3


def test_invalid_lookup_identifier_cannot_escape_archive(tmp_path: Path) -> None:
    repository = ManifestRepository(tmp_path)
    with pytest.raises(ManifestError):
        repository.lookup("../request-1")
