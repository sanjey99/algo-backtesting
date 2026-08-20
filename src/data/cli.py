"""Stable standard-library CLI for acquisition and report inspection."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd

from src.data.acquisition import AcquisitionService
from src.data.benchmark import run_deterministic_benchmark
from src.data.contracts import (
    AcquisitionRequest,
    ArtifactError,
    CacheError,
    ContractViolationError,
    DataAcquisitionError,
    InvalidRequestError,
    ManifestError,
    NoUsableDataError,
    ProviderExhaustedError,
    ProviderQuotaError,
    QualityError,
    json_safe,
)
from src.data.wiring import create_acquisition_service
from src.observability import configure_logging

EXIT_OK = 0
EXIT_REQUEST = 2
EXIT_PROVIDERS = 3
EXIT_QUALITY = 4
EXIT_ARTIFACT = 5
_SAFE_ACQUISITION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return a stable process exit code."""
    configure_logging()
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "benchmark":
            return _benchmark(args)
        service = create_acquisition_service(
            cache_dir=args.cache_dir,
            manifest_dir=args.manifest_dir,
        )
        if args.command == "acquire":
            return _acquire(args, service)
        return _inspect(args, service)
    except _UsageError:
        _emit_error("cli", "invalid_arguments", "Command arguments are invalid.")
        return EXIT_REQUEST
    except DataAcquisitionError as error:
        return _handle_typed_error(getattr(args, "command", "cli"), error)
    except Exception:
        _emit_error("cli", "artifact_failure", "The command could not publish its artifacts.")
        return EXIT_ARTIFACT


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m src.data.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--symbol", required=True)
    acquire.add_argument("--start", required=True)
    acquire.add_argument("--end", required=True)
    acquire.add_argument(
        "--source",
        choices=("auto", "yfinance", "alpha_vantage"),
        default="auto",
    )
    acquire.add_argument("--calendar", default="XNYS")
    acquire.add_argument("--refresh", action="store_true")
    acquire.add_argument("--no-cache", action="store_true")
    acquire.add_argument("--canonical", type=Path, required=True)
    acquire.add_argument("--report", type=Path, required=True)
    _storage_arguments(acquire)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--acquisition-id", required=True)
    _storage_arguments(inspect)

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--output", type=Path, required=True)
    return parser


def _storage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path("data/acquisition-reports"),
    )


def _acquire(args: argparse.Namespace, service: AcquisitionService) -> int:
    request = AcquisitionRequest(
        args.symbol,
        _parse_date(args.start),
        _parse_date(args.end),
        source=args.source,
        calendar=args.calendar,
        use_cache=not args.no_cache,
        refresh=args.refresh or args.no_cache,
    )
    result = service.acquire(request)
    _write_parquet(result.frame, args.canonical)
    _write_json(result.manifest.to_dict(), args.report)
    _emit(
        {
            "acquisition_id": result.manifest.acquisition_id,
            "artifacts": {
                "canonical": str(args.canonical),
                "report": str(args.report),
            },
            "command": "acquire",
            "status": result.manifest.status.value,
            "warnings": [warning.to_dict() for warning in result.warnings],
        }
    )
    return EXIT_OK


def _inspect(args: argparse.Namespace, service: AcquisitionService) -> int:
    if not _SAFE_ACQUISITION_ID.fullmatch(args.acquisition_id):
        raise InvalidRequestError("acquisition identifier is invalid")
    report = service.lookup_manifest(args.acquisition_id)
    if report is None:
        raise InvalidRequestError("acquisition report was not found")
    safe = json_safe(report)
    if not isinstance(safe, dict):
        raise ManifestError("acquisition report is invalid")
    _emit(
        {
            "acquisition_id": args.acquisition_id,
            "command": "inspect",
            "report": safe,
        }
    )
    return EXIT_OK


def _benchmark(args: argparse.Namespace) -> int:
    artifact = run_deterministic_benchmark()
    _write_json(artifact, args.output)
    deterministic = artifact.get("deterministic")
    scenarios = deterministic.get("scenarios", []) if isinstance(deterministic, Mapping) else []
    _emit(
        {
            "artifact": str(args.output),
            "command": "benchmark",
            "live_smoke": artifact.get("live_smoke") is not None,
            "scenarios": len(scenarios) if isinstance(scenarios, list) else 0,
        }
    )
    return EXIT_OK


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise InvalidRequestError("dates must use YYYY-MM-DD") from error


def _write_parquet(frame: pd.DataFrame, destination: Path) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise ArtifactError("canonical artifact requires a DataFrame")

    def write(path: Path) -> None:
        frame.copy(deep=True).to_parquet(path, index=False)

    _atomic_write(destination, write)


def _write_json(document: Mapping[str, Any], destination: Path) -> None:
    safe = json_safe(document)
    if not isinstance(safe, dict):
        raise ArtifactError("report artifact requires a JSON object")
    payload = f"{json.dumps(safe, sort_keys=True, separators=(',', ':'))}\n".encode()

    def write(path: Path) -> None:
        path.write_bytes(payload)

    _atomic_write(destination, write)


def _atomic_write(destination: Path, writer: Callable[[Path], None]) -> None:
    if destination.exists() and (destination.is_dir() or destination.is_symlink()):
        raise ArtifactError("artifact destination is not a regular file")
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        writer(temporary)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except ArtifactError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ArtifactError("artifact publication failed") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _handle_typed_error(command: str, error: DataAcquisitionError) -> int:
    if isinstance(error, InvalidRequestError | ContractViolationError):
        code, message, exit_code = (
            "invalid_request",
            "The acquisition request or configuration is invalid.",
            EXIT_REQUEST,
        )
    elif isinstance(error, ProviderExhaustedError | ProviderQuotaError):
        code, message, exit_code = (
            "providers_exhausted",
            "Market data providers were exhausted.",
            EXIT_PROVIDERS,
        )
    elif isinstance(error, NoUsableDataError | QualityError):
        code, message, exit_code = (
            "fatal_quality",
            "No usable market data satisfied quality requirements.",
            EXIT_QUALITY,
        )
    elif isinstance(error, CacheError | ManifestError | ArtifactError):
        code, message, exit_code = (
            "artifact_failure",
            "Market data artifacts could not be published.",
            EXIT_ARTIFACT,
        )
    else:
        code, message, exit_code = (
            "acquisition_failed",
            "Market data acquisition failed.",
            EXIT_ARTIFACT,
        )
    _emit_error(command, code, message, error.acquisition_id)
    return exit_code


def _emit_error(
    command: str,
    code: str,
    message: str,
    acquisition_id: str | None = None,
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if acquisition_id is not None:
        error["acquisition_id"] = acquisition_id
    _emit({"command": command, "error": error}, stream=sys.stderr)


def _emit(document: Mapping[str, Any], *, stream: Any = None) -> None:
    output = sys.stdout if stream is None else stream
    print(json.dumps(document, sort_keys=True, separators=(",", ":")), file=output)


if __name__ == "__main__":
    raise SystemExit(main())
