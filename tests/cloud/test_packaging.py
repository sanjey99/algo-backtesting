"""Offline contracts and smoke harness for the shared Lambda/Fargate image."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Never
from uuid import UUID

from src.cloud.contracts import (
    DatasetRef,
    ResearchRequest,
    RunRecord,
    RunSpec,
    RunStatus,
    canonical_json_bytes,
)
from src.cloud.prepare_handler import _run_spec_payload
from src.cloud.worker import execute_run
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "Dockerfile"
_DOCKERIGNORE = _ROOT / ".dockerignore"
_MAKEFILE = _ROOT / "Makefile"
_FIXTURE = Path(__file__).with_name("fixtures") / "spy-daily.parquet"
_RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
_REQUIRED_ARTIFACTS = frozenset(
    {
        "run-spec.json",
        "result.json",
        "trades.parquet",
        "equity-curve.parquet",
        "report.html",
        "checksums.json",
    }
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_image_contract_uses_locked_python_312_cloud_runtime_without_network_service() -> None:
    """Catches a mutable/root/networked image that cannot run both bounded modes safely."""
    dockerfile = _read(_DOCKERFILE)

    assert "FROM ghcr.io/astral-sh/uv:0.6.14@sha256:" in dockerfile
    assert "FROM python:3.12.11-slim-bookworm@sha256:" in dockerfile
    assert "docker/dockerfile:" not in dockerfile
    assert "-slim" in dockerfile
    assert "AS build" in dockerfile
    assert "AS runtime" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev --extra cloud --no-install-project" in dockerfile
    assert "pip install" not in dockerfile
    assert "COPY --from=build /opt/venv /opt/venv" in dockerfile
    assert "COPY src /app/src" in dockerfile
    assert "COPY . " not in dockerfile
    assert "COPY tests" not in dockerfile
    assert "EXPOSE" not in dockerfile
    assert 'HOME=/tmp' in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "chown -R root:root /app/src /opt/venv" in dockerfile
    assert "chmod -R a-w /app/src /opt/venv" in dockerfile
    assert 'ENTRYPOINT ["/opt/venv/bin/python", "-m", "awslambdaric"]' in dockerfile
    assert 'CMD ["src.cloud.results_handler.lambda_handler"]' in dockerfile


def test_build_context_excludes_sensitive_and_nonruntime_inputs() -> None:
    """Catches an accidental Docker build context leak of credentials or development data."""
    ignored = set(_read(_DOCKERIGNORE).splitlines())

    assert {
        ".git",
        ".codegraph",
        ".env",
        ".env.*",
        "tests",
        ".terraform",
        "*.tfstate",
        "*.tfplan",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".coverage",
        "htmlcov",
        "artifacts",
        "data",
    } <= ignored


def test_make_cloud_targets_keep_smoke_local_and_preserve_ecs_override_semantics() -> None:
    """Catches a Make target that needs credentials or lets RIC prefix ECS worker execution."""
    makefile = _read(_MAKEFILE)

    assert "cloud-test:" in makefile
    assert "cloud-smoke:" in makefile
    assert "cloud-verify:" in makefile
    assert "uv run --extra dev --extra cloud pytest tests/cloud/test_packaging.py -q" in makefile
    assert (
        "uv run --extra dev --extra cloud python tests/cloud/test_packaging.py --smoke"
        in makefile
    )
    assert "AWS_ACCESS_KEY_ID" not in makefile
    assert "AWS_SECRET_ACCESS_KEY" not in makefile

    # Terraform Task 10 must set both fields: image ENTRYPOINT is Lambda's RIC,
    # whereas ECS needs an unprefixed worker Python invocation.
    ecs_entry_point = ["/opt/venv/bin/python"]
    ecs_command = [
        "-m",
        "src.cloud.worker",
        "--run-spec-key",
        f"runs/v1/{_RUN_ID}/run-spec.json",
    ]
    assert ecs_entry_point + ecs_command == [
        "/opt/venv/bin/python",
        "-m",
        "src.cloud.worker",
        "--run-spec-key",
        f"runs/v1/{_RUN_ID}/run-spec.json",
    ]


def _run_spec(store: FakeObjectStore) -> RunSpec:
    fixture = _FIXTURE.read_bytes()
    dataset = DatasetRef(
        bucket="offline-artifacts",
        key="datasets/v1/offline-fixture/spy-daily.parquet",
        sha256=hashlib.sha256(fixture).hexdigest(),
        manifest_key="datasets/v1/offline-fixture/manifest.json",
        manifest_sha256="a" * 64,
        symbol="SPY",
        calendar="XNYS",
        interval="1d",
        start="2024-01-02",
        end="2024-04-02",
        acquisition_id="offline-fixture",
        completed_at=datetime(2024, 4, 2, tzinfo=UTC),
    )
    request = ResearchRequest.model_validate(
        {
            "symbol": "SPY",
            "start": "2024-01-02",
            "end": "2024-04-02",
            "strategy_key": "ma_crossover",
            "strategy_parameters": {"fast_period": 3, "slow_period": 10},
        }
    )
    run_spec = RunSpec.create(
        request=request,
        dataset=dataset,
        image_digest="sha256:" + "b" * 64,
        now=datetime(2024, 4, 3, tzinfo=UTC),
        run_id=UUID(_RUN_ID),
    )
    store.put(dataset.key, fixture, "application/vnd.apache.parquet")
    store.put(
        run_spec.run_spec_key,
        canonical_json_bytes(_run_spec_payload(run_spec)),
        "application/json",
    )
    return run_spec


def _run_offline_smoke(output_directory: Path) -> dict[str, tuple[int, str]]:
    """Run the real worker with local stores and write its six immutable artifacts."""
    output_directory.mkdir(parents=True, exist_ok=False)
    store = FakeObjectStore()
    repository = FakeRunRepository()
    run_spec = _run_spec(store)
    repository.create_pending(
        RunRecord(
            run_id=run_spec.run_id,
            status=RunStatus.PENDING,
            visibility=run_spec.request.visibility,
            dataset_key=run_spec.dataset.key,
            dataset_sha256=run_spec.dataset.sha256,
            run_spec_key=run_spec.run_spec_key,
            result_prefix=run_spec.result_prefix,
            image_digest=run_spec.image_digest,
            created_at=run_spec.created_at,
            expires_at=1_800_000_000,
        )
    )
    execute_run(
        run_spec.run_spec_key,
        object_store=store,
        run_repository=repository,
        clock=lambda: datetime(2024, 4, 3, 12, tzinfo=UTC),
    )

    artifact_bodies = {
        name: store.get(f"{run_spec.result_prefix}{name}", 32 * 1024 * 1024)
        for name in _REQUIRED_ARTIFACTS
        if name != "run-spec.json"
    }
    artifact_bodies["run-spec.json"] = store.get(run_spec.run_spec_key, 64 * 1024)
    assert set(artifact_bodies) == _REQUIRED_ARTIFACTS

    inventory: dict[str, tuple[int, str]] = {}
    for name, body in artifact_bodies.items():
        target = output_directory / name
        target.write_bytes(body)
        inventory[name] = (len(body), hashlib.sha256(body).hexdigest())
    return inventory


def test_offline_worker_smoke_writes_exact_artifact_set(tmp_path: Path) -> None:
    """Catches a packaging smoke harness that skips an artifact or uses AWS credentials."""
    inventory = _run_offline_smoke(tmp_path / "worker-output")

    assert set(inventory) == _REQUIRED_ARTIFACTS
    assert all(size > 0 and len(digest) == 64 for size, digest in inventory.values())
    result = json.loads((tmp_path / "worker-output" / "result.json").read_text(encoding="utf-8"))
    assert result["run_id"] == _RUN_ID
    assert all(value is None or math.isfinite(value) for value in result["metrics"].values())


def _main() -> Never:
    parser = argparse.ArgumentParser(description="Run the offline cloud worker packaging smoke.")
    parser.add_argument(
        "--smoke", action="store_true", help="run the local object-store worker harness"
    )
    parser.add_argument("--output-directory", type=Path)
    arguments = parser.parse_args()
    if not arguments.smoke:
        parser.error("--smoke is required")

    if arguments.output_directory is None:
        with tempfile.TemporaryDirectory(prefix="algo-cloud-smoke-") as temporary:
            inventory = _run_offline_smoke(Path(temporary) / "artifacts")
    else:
        inventory = _run_offline_smoke(arguments.output_directory)
    print(json.dumps(inventory, sort_keys=True))
    raise SystemExit(0)


if __name__ == "__main__":
    _main()
