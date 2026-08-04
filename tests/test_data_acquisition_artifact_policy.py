"""Committed-publication versus standalone report-archive failure policy."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.data.contracts import (
    REPORT_ARCHIVE_DEFERRED_WARNING,
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    AcquisitionWarning,
    ArtifactError,
    Provider,
    TransientProviderError,
)
from src.data.manifest import ManifestRepository
from tests.test_data_acquisition import NOW, FakeProvider, native_batch, service


class SelectiveArchiveFailureRepository(ManifestRepository):
    def __init__(self, root: Path, failed_ids: set[str], identifiers: list[str]) -> None:
        allocated = iter(identifiers)
        super().__init__(root, id_factory=lambda: next(allocated), clock=lambda: NOW)
        self.failed_ids = failed_ids

    def archive(self, manifest: AcquisitionManifest) -> Path:
        if manifest.acquisition_id in self.failed_ids:
            raise ArtifactError("injected secret=archive-failure")
        return super().archive(manifest)


def test_post_commit_archive_failure_returns_success_warning_and_pinned_fallback(
    tmp_path: Path,
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    repository = SelectiveArchiveFailureRepository(
        tmp_path / "reports",
        {"post-commit"},
        ["post-commit"],
    )
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(Provider.YFINANCE, item),
    )
    acquisition, store, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: provider},
        repository=repository,
    )

    result = acquisition.acquire(request)

    assert result.manifest.status is AcquisitionStatus.SUCCESS
    assert result.warnings == (REPORT_ARCHIVE_DEFERRED_WARNING,)
    assert result.warnings == (
        AcquisitionWarning(
            code="report_archive_deferred",
            message="Cache committed; request report archival was deferred.",
        ),
    )
    assert repository.lookup("post-commit") is None
    assert store.lookup_manifest("post-commit") == result.manifest.to_dict()
    pin = store.generation_namespace(request) / "pins" / "generation-1.json"
    assert pin.is_file()


def test_full_hit_archive_failure_is_standalone_artifact_error(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    repository = SelectiveArchiveFailureRepository(
        tmp_path / "reports",
        {"full-hit"},
        ["seed", "full-hit"],
    )
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(Provider.YFINANCE, item),
    )
    acquisition, store, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: provider},
        repository=repository,
    )
    acquisition.acquire(request)

    with pytest.raises(ArtifactError) as raised:
        acquisition.acquire(request)

    assert raised.value.acquisition_id == "full-hit"
    assert store.current_generation_id(request) == "generation-1"
    assert len(provider.requests) == 1


def test_service_side_post_commit_archive_failure_also_pins_and_warns(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    repository = SelectiveArchiveFailureRepository(
        tmp_path / "reports",
        {"service-archive"},
        ["service-archive"],
    )
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(Provider.YFINANCE, item),
    )
    acquisition, store, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: provider},
        repository=repository,
        archive_in_store=False,
    )

    result = acquisition.acquire(request)

    assert result.warnings == (REPORT_ARCHIVE_DEFERRED_WARNING,)
    assert store.lookup_manifest("service-archive") == result.manifest.to_dict()
    pin = store.generation_namespace(request) / "pins" / "generation-1.json"
    assert pin.is_file()


def test_post_commit_pin_failure_uses_active_generation_for_fallback_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    repository = SelectiveArchiveFailureRepository(
        tmp_path / "reports",
        {"post-commit"},
        ["post-commit"],
    )
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda item: native_batch(Provider.YFINANCE, item),
    )
    acquisition, store, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: provider},
        repository=repository,
    )

    def fail_pin(*_: object) -> None:
        raise OSError("injected pin failure")

    monkeypatch.setattr(store, "_pin", fail_pin)
    result = acquisition.acquire(request)

    assert result.warnings == (REPORT_ARCHIVE_DEFERRED_WARNING,)
    assert store.lookup_manifest("post-commit") == result.manifest.to_dict()
    repository.failed_ids.clear()
    assert store.maintain_manifest_archive() == ()
    assert repository.lookup("post-commit") == result.manifest.to_dict()


def test_failed_acquisition_archive_failure_is_standalone_artifact_error(
    tmp_path: Path,
) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    repository = SelectiveArchiveFailureRepository(
        tmp_path / "reports",
        {"failed-request"},
        ["failed-request"],
    )
    provider = FakeProvider(
        Provider.YFINANCE,
        lambda _: (_ for _ in ()).throw(TransientProviderError("provider down")),
    )
    acquisition, store, _ = service(
        tmp_path,
        {Provider.YFINANCE: lambda: provider},
        repository=repository,
    )

    with pytest.raises(ArtifactError) as raised:
        acquisition.acquire(request)

    assert raised.value.acquisition_id == "failed-request"
    assert store.current_generation_id(request) is None
