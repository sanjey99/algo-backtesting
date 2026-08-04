"""In-process CLI tests with injected acquisition services and no live network."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.data import cli
from src.data.contracts import (
    REPORT_ARCHIVE_DEFERRED_WARNING,
    AcquisitionWarning,
    ArtifactError,
    CachePublicationError,
    InvalidRequestError,
    NoUsableDataError,
    ProviderExhaustedError,
)
from tests.test_api_data_acquisition import FakeAcquisitionService, _result


def _install_service(monkeypatch: pytest.MonkeyPatch, service: FakeAcquisitionService) -> None:
    monkeypatch.setattr(cli, "create_acquisition_service", lambda **_: service)


def test_acquire_writes_atomic_artifacts_and_prints_acquisition_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeAcquisitionService(result=_result())
    _install_service(monkeypatch, service)
    canonical = tmp_path / "artifacts" / "bars.parquet"
    report = tmp_path / "artifacts" / "report.json"

    code = cli.main(
        [
            "acquire",
            "--symbol",
            "SPY",
            "--start",
            "2020-01-01",
            "--end",
            "2022-12-31",
            "--canonical",
            str(canonical),
            "--report",
            str(report),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--manifest-dir",
            str(tmp_path / "manifests"),
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "acquisition_id": "acquisition-123",
        "artifacts": {"canonical": str(canonical), "report": str(report)},
        "command": "acquire",
        "status": "partial_success",
        "warnings": [],
    }
    assert report.exists()
    assert pd.read_parquet(canonical).shape == (2, 11)
    assert json.loads(report.read_text())["acquisition_id"] == "acquisition-123"
    assert list(canonical.parent.glob(".*.tmp")) == []


def test_post_commit_archive_warning_keeps_cli_zero_and_prints_safe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeAcquisitionService(
        result=_result(warnings=(REPORT_ARCHIVE_DEFERRED_WARNING,))
    )
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "acquire",
            "--symbol",
            "SPY",
            "--start",
            "2020-01-01",
            "--end",
            "2022-12-31",
            "--canonical",
            str(tmp_path / "bars.parquet"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == [
        AcquisitionWarning(
            code="report_archive_deferred",
            message="Cache committed; request report archival was deferred.",
        ).to_dict()
    ]


def test_inspect_loads_existing_redacted_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _result().manifest.to_dict()
    report["metadata"] = {"token": "known-secret"}
    service = FakeAcquisitionService(report=report)
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "inspect",
            "--acquisition-id",
            "acquisition-123",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--manifest-dir",
            str(tmp_path / "manifests"),
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "inspect"
    assert output["acquisition_id"] == "acquisition-123"
    assert output["report"]["metadata"]["token"] == "[REDACTED]"
    assert "known-secret" not in json.dumps(output)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidRequestError("bad"), 2),
        (ProviderExhaustedError("exhausted"), 3),
        (NoUsableDataError("fatal"), 4),
        (CachePublicationError("cache"), 5),
        (ArtifactError("artifact"), 5),
    ],
)
def test_acquire_uses_stable_exit_codes_and_secret_free_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected: int,
) -> None:
    service = FakeAcquisitionService(error=error)
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "acquire",
            "--symbol",
            "SPY",
            "--start",
            "2020-01-01",
            "--end",
            "2020-01-03",
            "--canonical",
            str(tmp_path / "bars.parquet"),
            "--report",
            str(tmp_path / "report.json"),
        ]
    )

    assert code == expected
    output = json.loads(capsys.readouterr().err)
    assert output["command"] == "acquire"
    assert output["error"]["code"]
    assert "secret" not in json.dumps(output).lower()


def test_bad_arguments_return_two_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["acquire", "--symbol", "SPY"])

    assert code == 2
    output = json.loads(capsys.readouterr().err)
    assert output["error"]["code"] == "invalid_arguments"


def test_inspect_rejects_unsafe_identifier_as_request_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeAcquisitionService(report=_result().manifest.to_dict())
    _install_service(monkeypatch, service)

    code = cli.main(
        [
            "inspect",
            "--acquisition-id",
            "bad!",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--manifest-dir",
            str(tmp_path / "manifests"),
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_request"


def test_benchmark_writes_a_deterministic_artifact_without_live_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = {
        "deterministic": {"scenarios": [{"name": "cold_cache"}]},
        "live_smoke": None,
    }
    monkeypatch.setattr(cli, "run_deterministic_benchmark", lambda: artifact, raising=False)
    output = tmp_path / "benchmark.json"

    code = cli.main(["benchmark", "--output", str(output)])

    assert code == 0
    assert json.loads(output.read_text()) == artifact
    assert json.loads(capsys.readouterr().out) == {
        "artifact": str(output),
        "command": "benchmark",
        "live_smoke": False,
        "scenarios": 1,
    }
