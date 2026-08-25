"""Offline contracts and smoke harness for the shared Lambda/Fargate image."""

from __future__ import annotations

import argparse
import builtins
import importlib.util
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never
from uuid import UUID

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - exercised by the production image harness.
    pytest = None  # type: ignore[assignment]

from src.cloud import worker as worker_module
from src.cloud.contracts import (
    REQUIRED_ARTIFACTS,
    ChecksumsManifest,
    DatasetRef,
    ResearchRequest,
    RunRecord,
    RunSpec,
    RunStatus,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.prepare_handler import _run_spec_payload
from src.cloud.worker import execute_run

try:
    from tests.cloud.fakes import FakeObjectStore, FakeRunRepository
except ModuleNotFoundError:  # pragma: no cover - used by the mounted image harness.

    @dataclass(frozen=True, slots=True)
    class _PutCall:
        key: str
        body: bytes
        content_type: str

    class FakeObjectStore:
        """Minimal local fake bundled with the mounted smoke harness."""

        def __init__(self) -> None:
            self._objects: dict[str, bytes] = {}
            self._put_calls: list[_PutCall] = []

        @property
        def put_calls(self) -> tuple[_PutCall, ...]:
            return tuple(self._put_calls)

        def put(self, key: str, body: bytes, content_type: str) -> None:
            copied = bytes(body)
            existing = self._objects.get(key)
            if existing is not None and existing != copied:
                raise AssertionError("immutable smoke object collision")
            self._objects.setdefault(key, copied)
            self._put_calls.append(_PutCall(key, copied, content_type))

        def get(self, key: str, maximum_bytes: int) -> bytes:
            body = self._objects[key]
            if len(body) > maximum_bytes:
                raise AssertionError("smoke object exceeds its bounded read")
            return bytes(body)

    class FakeRunRepository:
        """The small injected state seam needed by the one-shot worker."""

        def __init__(self) -> None:
            self._record: RunRecord | None = None

        def create_pending(self, record: RunRecord) -> None:
            if self._record is not None or record.status is not RunStatus.PENDING:
                raise AssertionError("invalid smoke PENDING record")
            self._record = record

        def mark_running(self, run_id: str, started_at: datetime) -> None:
            if self._record is None or self._record.run_id != run_id:
                raise AssertionError("unknown smoke run")
            if self._record.status is not RunStatus.PENDING:
                raise AssertionError("smoke run is not pending")
            self._record = self._record.model_copy(
                update={"status": RunStatus.RUNNING, "started_at": started_at}
            )


_HARNESS_PATH = Path(__file__).resolve()
_ROOT = _HARNESS_PATH.parents[2] if len(_HARNESS_PATH.parents) > 2 else Path("/app")
_DOCKERFILE = _ROOT / "Dockerfile"
_DOCKERIGNORE = _ROOT / ".dockerignore"
_MAKEFILE = _ROOT / "Makefile"
_FIXTURE = Path(__file__).with_name("fixtures") / "spy-daily.parquet"
_RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
_PUBLICATION_SEQUENCE = (
    "run-spec.json",
    "result.json",
    "trades.parquet",
    "equity-curve.parquet",
    "report.html",
    "checksums.json",
)
_FROM_PATTERN = re.compile(
    r"^(?P<image>[a-z0-9./_-]+):(?P<tag>[^@\s]+)@sha256:(?P<digest>[0-9a-f]{64})"
    r"\s+AS\s+(?P<stage>[a-z][a-z0-9_-]*)$"
)


@dataclass(frozen=True, slots=True)
class _DockerInstruction:
    keyword: str
    arguments: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _docker_instructions(path: Path) -> tuple[_DockerInstruction, ...]:
    """Parse active logical Dockerfile instructions, retaining continuations."""
    logical_lines: list[str] = []
    pending = ""
    for raw_line in _read(path).splitlines():
        stripped = raw_line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            pending += f"{stripped[:-1].rstrip()} "
            continue
        logical_lines.append(f"{pending}{stripped}")
        pending = ""
    assert not pending, "Dockerfile has an unterminated continuation"
    return tuple(
        _DockerInstruction(keyword.upper(), arguments.strip())
        for line in logical_lines
        for keyword, _, arguments in (line.partition(" "),)
        if keyword
    )


def _dockerignore_rules(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in _read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_active_dockerfile_enforces_the_exact_pinned_runtime_contract() -> None:
    """Catches comments or extra active instructions that weaken the runtime image."""
    instructions = _docker_instructions(_DOCKERFILE)
    stages = tuple(instruction for instruction in instructions if instruction.keyword == "FROM")
    assert len(stages) == 3
    assert tuple(stage.arguments for stage in stages) == (
        "ghcr.io/astral-sh/uv:0.6.14@sha256:"
        "3362a526af7eca2fcd8604e6a07e873fb6e4286d8837cb753503558ce1213664 AS uv",
        "python:3.12.11-slim-bookworm@sha256:"
        "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS build",
        "python:3.12.11-slim-bookworm@sha256:"
        "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS runtime",
    )
    assert all(_FROM_PATTERN.fullmatch(stage.arguments) is not None for stage in stages)
    assert all(instruction.keyword != "EXPOSE" for instruction in instructions)
    copies = tuple(
        instruction.arguments for instruction in instructions if instruction.keyword == "COPY"
    )
    assert copies == (
        "--from=uv /uv /uvx /bin/",
        "pyproject.toml uv.lock ./",
        "--from=build /opt/venv /opt/venv",
        "src /app/src",
    )
    runtime_start = instructions.index(stages[2]) + 1
    assert tuple((item.keyword, item.arguments) for item in instructions[runtime_start:]) == (
        (
            "ENV",
            'PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp',
        ),
        (
            "RUN",
            "groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app "
            "&& mkdir -p /app /tmp /harness/fixtures && chmod 1777 /tmp",
        ),
        ("COPY", "--from=build /opt/venv /opt/venv"),
        ("COPY", "src /app/src"),
        ("RUN", "chown -R root:root /app/src /opt/venv && chmod -R a-w /app/src /opt/venv"),
        ("WORKDIR", "/app"),
        ("USER", "10001:10001"),
        ("ENTRYPOINT", '["/opt/venv/bin/python", "-m", "awslambdaric"]'),
        ("CMD", '["src.cloud.results_handler.lambda_handler"]'),
    )
    assert any(
        instruction.arguments == "uv sync --frozen --no-dev --extra cloud --no-install-project"
        for instruction in instructions
        if instruction.keyword == "RUN"
    )
    assert all("pip install" not in instruction.arguments for instruction in instructions)


def test_dockerignore_is_an_ordered_allowlist_that_never_reopens_sensitive_inputs() -> None:
    """Catches a later negation that reintroduces secrets after the narrow allowlist."""
    rules = _dockerignore_rules(_DOCKERIGNORE)
    allowlist = (
        "**",
        "!Dockerfile",
        "!.dockerignore",
        "!pyproject.toml",
        "!uv.lock",
        "!src/",
        "!src/**",
    )
    assert rules[: len(allowlist)] == allowlist
    assert all(not rule.startswith("!") for rule in rules[len(allowlist) :])
    assert {
        ".git",
        ".codegraph",
        ".env",
        ".env.*",
        "**/.env",
        "**/.env.*",
        ".aws",
        "credentials",
        "*.pem",
        "*.key",
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
    } <= set(rules[len(allowlist) :])


def test_make_cloud_targets_are_frozen_and_container_smoke_enforces_runtime_policy() -> None:
    """Catches a non-reproducible target or a container smoke without security controls."""
    makefile = _read(_MAKEFILE)

    assert "cloud-test:" in makefile
    assert "cloud-smoke:" in makefile
    assert "cloud-verify:" in makefile
    assert "cloud-container-smoke:" in makefile
    assert (
        "uv run --frozen --extra dev --extra cloud pytest tests/cloud/test_packaging.py -q"
        in makefile
    )
    assert (
        "uv run --frozen --extra dev --extra cloud python tests/cloud/test_packaging.py --smoke"
        in makefile
    )
    assert "docker build --platform linux/amd64 -t $(CLOUD_IMAGE) ." in makefile
    assert "--network none" in makefile
    assert "--read-only" in makefile
    assert "--user 10001:10001" in makefile
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m" in makefile
    assert "-e PYTHONPATH=/harness:/app" in makefile
    assert "readonly" in makefile
    assert "--container-smoke" in makefile
    assert "--verify-container-artifacts" in makefile
    assert "docker exec" in makefile
    assert "tarfile.open" in makefile
    assert "AWS_ACCESS_KEY_ID" not in makefile
    assert "AWS_SECRET_ACCESS_KEY" not in makefile


def test_mounted_harness_imports_without_the_dev_test_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a production image smoke harness that leaks a dev-only pytest dependency."""
    real_import = builtins.__import__

    def reject_pytest(name: str, *args: object, **kwargs: object) -> object:
        if name == "pytest":
            raise ModuleNotFoundError("pytest is intentionally absent from the runtime image")
        return real_import(name, *args, **kwargs)

    module_name = "_image_smoke_harness"
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as context:
        context.setitem(sys.modules, module_name, module)
        context.setattr(builtins, "__import__", reject_pytest)
        spec.loader.exec_module(module)
    assert module._main is not None


def _run_spec(store: FakeObjectStore) -> RunSpec:
    fixture = _FIXTURE.read_bytes()
    dataset = DatasetRef(
        bucket="offline-artifacts",
        key="datasets/v1/offline-fixture/spy-daily.parquet",
        sha256=sha256_hex(fixture),
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


def _validate_artifact_bodies(bodies: dict[str, bytes]) -> dict[str, tuple[int, str]]:
    """Verify the exact finalizer-facing artifact set without filtering expected names first."""
    assert set(bodies) == set(_PUBLICATION_SEQUENCE)
    manifest = ChecksumsManifest.model_validate_json(bodies["checksums.json"])
    declared = {artifact.name: artifact for artifact in manifest.artifacts}
    assert set(declared) == REQUIRED_ARTIFACTS
    assert len(declared) == len(manifest.artifacts) == len(REQUIRED_ARTIFACTS)
    assert "checksums.json" not in declared
    for name, digest in declared.items():
        body = bodies[name]
        assert digest.byte_length == len(body)
        assert digest.sha256 == sha256_hex(body)
    return {name: (len(body), sha256_hex(body)) for name, body in sorted(bodies.items())}


def _published_artifact_bodies(store: FakeObjectStore, run_spec: RunSpec) -> dict[str, bytes]:
    """Read exactly what the worker actually published, including ordering and no extras."""
    calls = store.put_calls
    expected_keys = (
        run_spec.dataset.key,
        *(f"{run_spec.result_prefix}{name}" for name in _PUBLICATION_SEQUENCE),
    )
    assert tuple(call.key for call in calls) == expected_keys
    assert (
        tuple(call.key.removeprefix(run_spec.result_prefix) for call in calls[1:])
        == _PUBLICATION_SEQUENCE
    )
    return {
        name: store.get(f"{run_spec.result_prefix}{name}", 32 * 1024 * 1024)
        for name in _PUBLICATION_SEQUENCE
    }


def _run_offline_smoke(output_directory: Path) -> dict[str, tuple[int, str]]:
    """Run the real worker and copy its validated publication set to a fresh directory."""
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
    bodies = _published_artifact_bodies(store, run_spec)
    inventory = _validate_artifact_bodies(bodies)
    for name, body in bodies.items():
        (output_directory / name).write_bytes(body)
    return inventory


def _verify_copied_artifacts(artifact_directory: Path) -> dict[str, tuple[int, str]]:
    """Independently verify the unfiltered container receipt copied to the host."""
    assert artifact_directory.is_dir()
    paths = tuple(artifact_directory.iterdir())
    assert all(path.is_file() for path in paths)
    return _validate_artifact_bodies({path.name: path.read_bytes() for path in paths})


def _runtime_worker_path() -> str:
    path = Path(worker_module.__file__).resolve()
    assert path.is_relative_to(Path("/app/src")), f"worker imported from {path}, not /app/src"
    return str(path)


def _assert_container_filesystem_policy() -> dict[str, str]:
    """Prove runtime policy supplies the bounded writable area, not Dockerfile permissions."""
    try:
        Path("/var/tmp/.write-probe").touch()
    except OSError:
        var_tmp = "denied"
    else:
        raise AssertionError("/var/tmp is writable without a read-only root filesystem")
    Path("/tmp/.write-probe").touch()
    return {"tmp_write": "succeeded", "var_tmp_write": var_tmp}


def test_offline_worker_smoke_writes_exact_artifact_set(tmp_path: Path) -> None:
    """Catches a successful worker run that lacks a complete verified output contract."""
    artifact_directory = tmp_path / "worker-output"
    inventory = _run_offline_smoke(artifact_directory)

    assert tuple(inventory) == tuple(sorted(_PUBLICATION_SEQUENCE))
    assert all(size > 0 and len(digest) == 64 for size, digest in inventory.values())
    result = json.loads((artifact_directory / "result.json").read_text(encoding="utf-8"))
    assert result["run_id"] == _RUN_ID
    assert all(value is None or math.isfinite(value) for value in result["metrics"].values())
    assert _verify_copied_artifacts(artifact_directory) == inventory


def test_offline_worker_smoke_rejects_an_extra_published_result_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a harness that selects six expected files while ignoring worker extras."""
    real_execute_run = execute_run

    def publish_extra(*args: object, **kwargs: object) -> None:
        real_execute_run(*args, **kwargs)
        store = kwargs["object_store"]
        assert isinstance(store, FakeObjectStore)
        store.put(f"runs/v1/{_RUN_ID}/debug.json", b"{}", "application/json")

    monkeypatch.setattr("tests.cloud.test_packaging.execute_run", publish_extra)
    with pytest.raises(AssertionError):
        _run_offline_smoke(tmp_path / "worker-output")


def test_offline_worker_smoke_rejects_a_tampered_checksums_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches a smoke verifier that never parses the finalizer-facing manifest."""
    real_execute_run = execute_run

    def tamper_manifest(*args: object, **kwargs: object) -> None:
        real_execute_run(*args, **kwargs)
        store = kwargs["object_store"]
        assert isinstance(store, FakeObjectStore)
        store._objects[f"runs/v1/{_RUN_ID}/checksums.json"] = b"{}"

    monkeypatch.setattr("tests.cloud.test_packaging.execute_run", tamper_manifest)
    with pytest.raises(Exception):
        _run_offline_smoke(tmp_path / "worker-output")


def _main() -> Never:
    parser = argparse.ArgumentParser(description="Run the offline cloud worker packaging smoke.")
    parser.add_argument(
        "--smoke", action="store_true", help="run the local object-store worker harness"
    )
    parser.add_argument(
        "--container-smoke", action="store_true", help="hold validated /tmp artifacts for docker cp"
    )
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--verify-container-artifacts", type=Path)
    arguments = parser.parse_args()
    if arguments.verify_container_artifacts is not None:
        print(
            json.dumps(
                _verify_copied_artifacts(arguments.verify_container_artifacts), sort_keys=True
            )
        )
        raise SystemExit(0)
    if arguments.smoke == arguments.container_smoke:
        parser.error("choose exactly one of --smoke or --container-smoke")
    if arguments.container_smoke:
        if arguments.output_directory is None:
            parser.error("--container-smoke requires --output-directory")
        inventory = _run_offline_smoke(arguments.output_directory)
        print(
            json.dumps(
                {
                    "artifact_inventory": inventory,
                    "filesystem": _assert_container_filesystem_policy(),
                    "ready_for_copy": True,
                    "worker_path": _runtime_worker_path(),
                },
                sort_keys=True,
            )
        )
        release = Path("/tmp/release")
        while not release.exists():
            time.sleep(0.1)
    elif arguments.output_directory is None:
        with tempfile.TemporaryDirectory(prefix="algo-cloud-smoke-") as temporary:
            print(json.dumps(_run_offline_smoke(Path(temporary) / "artifacts"), sort_keys=True))
    else:
        print(json.dumps(_run_offline_smoke(arguments.output_directory), sort_keys=True))
    raise SystemExit(0)


if __name__ == "__main__":
    _main()
