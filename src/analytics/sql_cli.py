"""Command-line adapter for SQL analytics services."""
from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

from sqlalchemy.exc import SQLAlchemyError

from src.analytics.sql_artifacts import (
    ArtifactExistsError,
    ArtifactPathError,
    preflight_artifact_destinations,
    write_comparison_bundle,
    write_json_artifact,
)
from src.analytics.sql_benchmark import BenchmarkConfig, BenchmarkRunner
from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import ComparisonFilters, ComparisonMetadata, QueryId
from src.analytics.sql_service import (
    AnalyticsService,
    ContractValidationError,
    IntegrityFailureError,
    IntegrityService,
    RunNotFoundError,
)
from src.db.database import create_db_engine

_SUCCESS = 0
_DATABASE_FAILURE = 1
_INVALID_INPUT = 2
_UNKNOWN_RUN = 3
_CONTRACT_FAILURE = 4
_INTEGRITY_FAILURE = 5
_DEFAULT_TOLERANCE = 0.02
_CONTRACT_VERSION = "1.0"
_METADATA_SCHEMA_VERSION = "1.0"


class _SelectedIntegrityError(RuntimeError):
    """Signal that selected comparison rows have fatal integrity findings."""


class _ValidationIntegrityError(RuntimeError):
    """Signal that a written database-wide validation report has fatal findings."""


class _InvalidCliInputError(ValueError):
    """Signal cross-argument validation that argparse cannot express directly."""


class _SanitizedArgumentParser(argparse.ArgumentParser):
    """Convert argparse failures into safe messages handled by ``main``."""

    def error(self, message: str) -> NoReturn:
        raise _InvalidCliInputError(_sanitize_parse_error(message))


def main(argv: Sequence[str] | None = None) -> int:
    """Run a SQL analytics command and return its stable process exit code."""
    parser = _build_parser()
    verbose = "--verbose" in (argv if argv is not None else sys.argv[1:])
    try:
        try:
            arguments = parser.parse_args(argv)
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else _INVALID_INPUT
        verbose = bool(arguments.verbose)
        if arguments.command == "compare":
            return _run_compare(arguments)
        if arguments.command == "validate":
            return _run_validate(arguments)
        if arguments.command == "benchmark":
            return _run_benchmark(arguments)
        raise _InvalidCliInputError("invalid command-line input")
    except ArtifactExistsError as error:
        return _error_exit(_INVALID_INPUT, "artifact destination already exists", error, verbose)
    except ArtifactPathError as error:
        return _error_exit(_INVALID_INPUT, "invalid artifact path", error, verbose)
    except _InvalidCliInputError as error:
        return _error_exit(_INVALID_INPUT, str(error), error, verbose)
    except RunNotFoundError as error:
        return _error_exit(_UNKNOWN_RUN, "requested run was not found", error, verbose)
    except ContractValidationError as error:
        return _error_exit(_CONTRACT_FAILURE, "result contract validation failed", error, verbose)
    except (IntegrityFailureError, _SelectedIntegrityError, _ValidationIntegrityError) as error:
        return _error_exit(_INTEGRITY_FAILURE, "integrity validation failed", error, verbose)
    except ValueError as error:
        return _error_exit(_INVALID_INPUT, "invalid input", error, verbose)
    except (SQLAlchemyError, OSError) as error:
        return _error_exit(_DATABASE_FAILURE, "database operation failed", error, verbose)
    except Exception as error:
        return _error_exit(_DATABASE_FAILURE, "operation failed", error, verbose)


def _build_parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(prog="sql-analytics")
    parser.add_argument("--verbose", action="store_true", help="show exception tracebacks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare = subparsers.add_parser("compare", help="export a validated strategy comparison")
    _allow_subcommand_verbose(compare)
    compare.add_argument("--database", required=True, type=_existing_database_path)
    compare.add_argument("--symbol", required=True, type=_nonempty_text)
    compare.add_argument("--start", required=True, type=_iso_date)
    compare.add_argument("--end", required=True, type=_iso_date)
    compare.add_argument("--strategy", type=_nonempty_text)
    compare.add_argument("--csv", required=True, type=_output_path)
    compare.add_argument("--metadata", required=True, type=_output_path)
    compare.add_argument("--diagnostic-override", action="store_true")
    compare.add_argument("--force", action="store_true")

    validate = subparsers.add_parser("validate", help="write a database integrity report")
    _allow_subcommand_verbose(validate)
    validate.add_argument("--database", required=True, type=_existing_database_path)
    validate.add_argument("--out", required=True, type=_output_path)
    validate.add_argument("--tolerance", type=_nonnegative_float, default=_DEFAULT_TOLERANCE)
    validate.add_argument("--sample-limit", type=_sample_limit, default=20)
    validate.add_argument("--force", action="store_true")

    benchmark = subparsers.add_parser(
        "benchmark", help="write deterministic SQLite benchmark evidence"
    )
    _allow_subcommand_verbose(benchmark)
    benchmark.add_argument("--seed", type=_integer, default=42)
    benchmark.add_argument("--runs", type=_integer, default=150)
    benchmark.add_argument("--equity-points-per-run", type=_integer, default=2_520)
    benchmark.add_argument("--trades-per-run", type=_integer, default=40)
    benchmark.add_argument("--warmups", type=_integer, default=3)
    benchmark.add_argument("--repetitions", type=_integer, default=15)
    benchmark.add_argument("--database-out", required=True, type=_output_path)
    benchmark.add_argument("--out", required=True, type=_output_path)
    return parser


def _allow_subcommand_verbose(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )


def _run_compare(arguments: argparse.Namespace) -> int:
    if arguments.start >= arguments.end:
        raise _InvalidCliInputError("start date must be before end date")
    _reject_database_destination_collisions(
        arguments.database, (arguments.csv, arguments.metadata)
    )
    preflight_artifact_destinations(
        (arguments.csv, arguments.metadata), bool(arguments.force)
    )
    start = datetime.combine(arguments.start, datetime.min.time())
    end = datetime.combine(arguments.end, datetime.min.time())
    filters = ComparisonFilters(
        symbol=arguments.symbol,
        start_date=start,
        end_date=end,
        strategy_name=arguments.strategy,
    )
    engine = create_db_engine(_sqlite_url(arguments.database))
    try:
        frame = AnalyticsService(engine).compare_runs(filters)
        run_ids = tuple(str(run_id) for run_id in frame["run_id"].tolist())
        failures = IntegrityService(engine).failures_for_run_ids(run_ids, _DEFAULT_TOLERANCE)
        if failures and not arguments.diagnostic_override:
            raise _SelectedIntegrityError("selected rows have fatal integrity findings")
        metadata = _comparison_metadata(
            database=arguments.database,
            filters=filters,
            row_count=len(frame.index),
            columns=tuple(str(column) for column in frame.columns),
            diagnostic_override=bool(arguments.diagnostic_override),
        )
        write_comparison_bundle(
            frame,
            metadata,
            arguments.csv,
            arguments.metadata,
            bool(arguments.force),
        )
    finally:
        engine.dispose()
    return _SUCCESS


def _run_validate(arguments: argparse.Namespace) -> int:
    _reject_database_destination_collisions(arguments.database, (arguments.out,))
    preflight_artifact_destinations((arguments.out,), bool(arguments.force))
    engine = create_db_engine(_sqlite_url(arguments.database))
    try:
        report = IntegrityService(engine).validate(arguments.tolerance, arguments.sample_limit)
        write_json_artifact(report, arguments.out, bool(arguments.force))
    finally:
        engine.dispose()
    if report.has_failures:
        raise _ValidationIntegrityError("validation report contains fatal findings")
    return _SUCCESS


def _run_benchmark(arguments: argparse.Namespace) -> int:
    config = BenchmarkConfig(
        seed=arguments.seed,
        run_count=arguments.runs,
        equity_points_per_run=arguments.equity_points_per_run,
        trades_per_run=arguments.trades_per_run,
        warmups=arguments.warmups,
        repetitions=arguments.repetitions,
    )
    database_out = arguments.database_out.expanduser().absolute()
    report_out = arguments.out.expanduser().absolute()
    preflight_artifact_destinations((database_out, report_out), force=False)
    report = BenchmarkRunner(database_out).run(config)
    write_json_artifact(report, report_out, force=False)
    fixture = report.fixture
    if fixture is None:
        raise RuntimeError("benchmark report is missing fixture metadata")
    print(
        "compare filters: "
        f"--database {database_out} "
        f"--symbol {fixture.symbol} "
        f"--start {fixture.start_date.date().isoformat()} "
        f"--end {fixture.end_date.date().isoformat()}"
    )
    return _SUCCESS


def _comparison_metadata(
    *,
    database: Path,
    filters: ComparisonFilters,
    row_count: int,
    columns: tuple[str, ...],
    diagnostic_override: bool,
) -> ComparisonMetadata:
    loaded = QueryCatalogue().load(QueryId.STRATEGY_RUN_COMPARISON)
    return ComparisonMetadata(
        schema_version=_METADATA_SCHEMA_VERSION,
        generated_at=datetime.now(UTC),
        query_id=QueryId.STRATEGY_RUN_COMPARISON,
        sql_sha256=loaded.sha256,
        bound_params={
            "symbol": filters.symbol,
            "start_date": _sqlite_datetime_text(filters.start_date),
            "end_date": _sqlite_datetime_text(filters.end_date),
            "strategy_name": filters.strategy_name,
        },
        database_identifier=str(database),
        row_count=row_count,
        ordered_columns=columns,
        contract_version=_CONTRACT_VERSION,
        contract_valid=True,
        validation_report_path="",
        diagnostic_override=diagnostic_override,
    )


def _sqlite_datetime_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _sqlite_url(database: Path) -> str:
    encoded_path = quote(str(database), safe="/")
    return f"sqlite:///file:{encoded_path}?uri=true"


def _existing_database_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError("database must be an existing SQLite file")
    return path


def _output_path(value: str) -> Path:
    if not value:
        raise argparse.ArgumentTypeError("output path must not be empty")
    path = Path(value).expanduser()
    if path.exists() and not path.is_file():
        raise argparse.ArgumentTypeError("output path must not be a directory")
    return path


def _reject_database_destination_collisions(
    database: Path, destinations: tuple[Path, ...]
) -> None:
    if any(_paths_refer_to_same_file(database, destination) for destination in destinations):
        raise _InvalidCliInputError("artifact destination conflicts with database")


def _paths_refer_to_same_file(database: Path, destination: Path) -> bool:
    try:
        if database.resolve(strict=False) == destination.resolve(strict=False):
            return True
        if not destination.exists():
            return False
        return destination.samefile(database)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ArtifactPathError("Artifact destination identity could not be verified") from error


def _nonempty_text(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use ISO YYYY-MM-DD format") from error
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("date must use ISO YYYY-MM-DD format")
    return parsed


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("tolerance must be numeric") from error
    if not isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("tolerance must be finite and non-negative")
    return parsed


def _sample_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("sample limit must be an integer") from error
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("sample limit must be from 1 to 100")
    return parsed


def _integer(value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error


def _sanitize_parse_error(message: str) -> str:
    if message.startswith("unrecognized arguments:") or "invalid choice:" in message:
        return "invalid command-line input"
    return message


def _error_exit(code: int, message: str, error: BaseException, verbose: bool) -> int:
    if verbose:
        traceback.print_exception(error, file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
