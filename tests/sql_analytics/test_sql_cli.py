"""CLI integration tests for validated SQL analytics artifacts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from src.analytics import sql_cli
from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import QueryId
from src.analytics.sql_service import AnalyticsService, ContractValidationError, RunNotFoundError
from src.db.database import create_db_engine
from src.db.tables import MetricRecord


def _database_path(engine: Engine) -> Path:
    database = engine.url.database
    assert database is not None
    return Path(database)


def _compare_args(database: Path, csv_path: Path, metadata_path: Path) -> list[str]:
    return [
        "compare",
        "--database",
        str(database),
        "--symbol",
        "SPY",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-31",
        "--csv",
        str(csv_path),
        "--metadata",
        str(metadata_path),
    ]


def _snapshot(engine: Engine) -> tuple[tuple[str, int], ...]:
    names = tuple(sorted(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        return tuple(
            (name, int(connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar_one()))
            for name in names
        )


def test_required_subcommand_and_deferred_benchmark_return_invalid_input() -> None:
    """Removing required subparsers or accidentally treating the stub as success breaks exit 2."""
    assert sql_cli.main([]) == 2
    assert sql_cli.main(["benchmark"]) == 2


@pytest.mark.parametrize(
    "args",
    [
        ["compare", "--database", "missing.db", "--symbol", "SPY", "--start", "bad"],
        ["compare", "--database", "missing.db", "--symbol", "SPY", "--start", "2024-1-1"],
    ],
)
def test_compare_rejects_non_iso_dates(args: list[str]) -> None:
    """Relaxing strict ISO parsing must retain exit 2 rather than reaching the database."""
    assert sql_cli.main(args) == 2


def test_compare_rejects_reversed_range_and_non_file_database(tmp_path: Path) -> None:
    """Date ordering and the SQLite file boundary are validated before service execution."""
    database = tmp_path / "db.sqlite"
    database.write_bytes(b"")
    output = tmp_path / "out"
    base = _compare_args(database, output / "x.csv", output / "x.json")
    start_index = base.index("--start") + 1
    end_index = base.index("--end") + 1
    base[start_index] = "2024-01-31"
    base[end_index] = "2024-01-31"
    assert sql_cli.main(base) == 2

    base[start_index] = "2024-02-01"
    base[end_index] = "2024-01-31"
    assert sql_cli.main(base) == 2

    database.unlink()
    database.mkdir()
    assert sql_cli.main(_compare_args(database, output / "x.csv", output / "x.json")) == 2


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--tolerance", "-0.1"),
        ("--tolerance", "nan"),
        ("--sample-limit", "0"),
        ("--sample-limit", "101"),
    ],
)
def test_validate_rejects_invalid_numeric_bounds(
    analytics_db: Engine, tmp_path: Path, option: str, value: str
) -> None:
    """Invalid numeric values must be exit 2 and must not create a report."""
    report_path = tmp_path / "validation.json"
    code = sql_cli.main(
        [
            "validate",
            "--database",
            str(_database_path(analytics_db)),
            "--out",
            str(report_path),
            option,
            value,
        ]
    )
    assert code == 2
    assert not report_path.exists()


def test_compare_converts_database_path_and_writes_exact_metadata(
    analytics_db: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong URL conversion or metadata provenance fields must fail this end-to-end boundary."""
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    calls: list[str] = []
    real_create = create_db_engine

    def capture_url(database_url: str) -> Engine:
        calls.append(database_url)
        return real_create(database_url)

    monkeypatch.setattr(sql_cli, "create_db_engine", capture_url)

    code = sql_cli.main(_compare_args(_database_path(analytics_db), csv_path, metadata_path))

    assert code == 0
    expected_database = str(_database_path(analytics_db).resolve())
    assert calls == [f"sqlite:///file:{expected_database}?uri=true"]
    rows = csv_path.read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("run_id,strategy_name,symbol,start_date,end_date,")
    assert len(rows) == 3
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "bound_params": {
            "end_date": "2024-01-31 00:00:00.000000",
            "start_date": "2024-01-01 00:00:00.000000",
            "strategy_name": None,
            "symbol": "SPY",
        },
        "contract_valid": True,
        "contract_version": "1.0",
        "database_identifier": expected_database,
        "diagnostic_override": False,
        "generated_at": metadata["generated_at"],
        "ordered_columns": rows[0].split(","),
        "query_id": "strategy_run_comparison",
        "row_count": 2,
        "schema_version": "1.0",
        "sql_sha256": QueryCatalogue().load(QueryId.STRATEGY_RUN_COMPARISON).sha256,
        "validation_report_path": "",
    }
    assert metadata["generated_at"].endswith("+00:00")
    assert "@" not in metadata["database_identifier"]


def test_compare_escapes_sqlite_filename_url_metacharacters(
    analytics_db: Engine, tmp_path: Path
) -> None:
    """A question mark in a valid filename must not become a SQLAlchemy URL query."""
    database = tmp_path / "analytics?copy.db"
    shutil.copyfile(_database_path(analytics_db), database)
    csv_path = tmp_path / "escaped.csv"
    metadata_path = tmp_path / "escaped.json"

    assert sql_cli.main(_compare_args(database, csv_path, metadata_path)) == 0
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["database_identifier"] == str(
        database.resolve()
    )


def test_compare_empty_cohort_is_success_with_header_only_artifact(
    analytics_db: Engine, tmp_path: Path
) -> None:
    """Treating a valid empty selection as an error would violate exit 0 and the CSV contract."""
    csv_path = tmp_path / "empty.csv"
    metadata_path = tmp_path / "empty.json"
    args = _compare_args(_database_path(analytics_db), csv_path, metadata_path)
    args[args.index("--symbol") + 1] = "MISSING"

    assert sql_cli.main(args) == 0
    assert len(csv_path.read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["row_count"] == 0


def test_existing_comparison_artifact_is_exit_2_without_partial_overwrite(
    analytics_db: Engine, tmp_path: Path
) -> None:
    """Artifact preflight preserves user output and the stable invalid-input exit."""
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    csv_path.write_text("owned", encoding="utf-8")

    assert sql_cli.main(_compare_args(_database_path(analytics_db), csv_path, metadata_path)) == 2
    assert csv_path.read_text(encoding="utf-8") == "owned"
    assert not metadata_path.exists()


def test_validate_writes_versioned_report_and_force_controls_overwrite(
    analytics_db: Engine, tmp_path: Path
) -> None:
    """Validation must persist its report before returning status and protect existing output."""
    out = tmp_path / "validation.json"
    args = [
        "validate",
        "--database",
        str(_database_path(analytics_db)),
        "--out",
        str(out),
        "--tolerance",
        "0.02",
        "--sample-limit",
        "2",
    ]

    assert sql_cli.main(args) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["schema_version"] == "1.0"
    assert report["tolerance"] == 0.02
    assert report["generated_at"].endswith("+00:00")
    assert isinstance(report["findings"], list)
    original = out.read_bytes()
    assert sql_cli.main(args) == 2
    assert out.read_bytes() == original
    assert sql_cli.main([*args, "--force"]) == 0


def test_validate_writes_failed_report_before_returning_exit_5(
    legacy_analytics_db: Engine, tmp_path: Path
) -> None:
    """Fatal validation findings remain inspectable in the report returned with exit 5."""
    with Session(legacy_analytics_db) as session:
        session.execute(
            update(MetricRecord)
            .where(MetricRecord.backtest_id == "run-ma")
            .where(MetricRecord.metric_name == "total_return")
            .values(metric_value=0.50)
        )
        session.commit()
    out = tmp_path / "failed-validation.json"

    code = sql_cli.main(
        [
            "validate",
            "--database",
            str(_database_path(legacy_analytics_db)),
            "--out",
            str(out),
        ]
    )

    assert code == 5
    report = json.loads(out.read_text(encoding="utf-8"))
    assert any(finding["severity"] == "FAIL" for finding in report["findings"])


def test_integrity_failure_blocks_writes_and_override_is_recorded(
    legacy_analytics_db: Engine, tmp_path: Path
) -> None:
    """A selected fatal mismatch must block both files unless an auditable override is explicit."""
    with Session(legacy_analytics_db) as session:
        session.execute(
            update(MetricRecord)
            .where(MetricRecord.backtest_id == "run-ma")
            .where(MetricRecord.metric_name == "total_return")
            .values(metric_value=0.50)
        )
        session.commit()
    csv_path = tmp_path / "comparison.csv"
    metadata_path = tmp_path / "comparison.json"
    args = _compare_args(_database_path(legacy_analytics_db), csv_path, metadata_path)

    assert sql_cli.main(args) == 5
    assert not csv_path.exists()
    assert not metadata_path.exists()
    assert sql_cli.main([*args, "--diagnostic-override"]) == 0
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["diagnostic_override"] is True


def test_all_documented_exception_classes_map_to_stable_sanitized_exits(
    analytics_db: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exception details cannot alter codes 1, 3, and 4 or leak in normal stderr."""
    args = _compare_args(
        _database_path(analytics_db), tmp_path / "comparison.csv", tmp_path / "comparison.json"
    )
    cases: tuple[tuple[Exception, int, str], ...] = (
        (
            DatabaseError("statement-with-secret", {}, RuntimeError("credential=secret")),
            1,
            "database operation failed",
        ),
        (RunNotFoundError("private-run-id"), 3, "requested run was not found"),
        (ContractValidationError("private-column"), 4, "result contract validation failed"),
    )

    for error, expected_code, expected_message in cases:
        def fail_compare(
            self: AnalyticsService, filters: object, *, raised: Exception = error
        ) -> object:
            raise raised

        monkeypatch.setattr(AnalyticsService, "compare_runs", fail_compare)
        assert sql_cli.main(args) == expected_code
        stderr = capsys.readouterr().err
        assert expected_message in stderr
        assert str(error) not in stderr
        assert "secret" not in stderr


def test_verbose_includes_traceback_for_diagnostics(
    analytics_db: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The explicit verbose flag is the only path that exposes exception diagnostics."""
    def fail_compare(self: AnalyticsService, filters: object) -> object:
        raise ContractValidationError("controlled verbose detail")

    monkeypatch.setattr(AnalyticsService, "compare_runs", fail_compare)
    args = [
        "--verbose",
        *_compare_args(
            _database_path(analytics_db), tmp_path / "comparison.csv", tmp_path / "comparison.json"
        ),
    ]
    assert sql_cli.main(args) == 4
    stderr = capsys.readouterr().err
    assert "Traceback" in stderr
    assert "controlled verbose detail" in stderr


@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("--symbol", "SPY'; DROP TABLE backtest_runs; --"),
        ("--strategy", "moving_average'; DELETE FROM trades; --"),
    ],
)
def test_symbol_and_strategy_injection_values_are_bound_and_read_only(
    analytics_db: Engine, tmp_path: Path, field: str, hostile: str
) -> None:
    """Interpolating either external value into SQL would change this database snapshot."""
    before = _snapshot(analytics_db)
    csv_path = tmp_path / f"{field[2:]}.csv"
    metadata_path = tmp_path / f"{field[2:]}.json"
    args = _compare_args(_database_path(analytics_db), csv_path, metadata_path)
    if field == "--symbol":
        args[args.index(field) + 1] = hostile
    else:
        args.extend([field, hostile])

    assert sql_cli.main(args) == 0
    assert _snapshot(analytics_db) == before
    bind_name = "strategy_name" if field == "--strategy" else "symbol"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["bound_params"][bind_name] == hostile


@pytest.mark.parametrize("option", ["--sort", "--query-id"])
def test_compare_rejects_arbitrary_sort_and_query_identifiers(
    analytics_db: Engine, tmp_path: Path, option: str
) -> None:
    """Adding open query/sort inputs would bypass the closed catalogue boundary."""
    args = _compare_args(
        _database_path(analytics_db), tmp_path / "comparison.csv", tmp_path / "comparison.json"
    )
    assert sql_cli.main([*args, option, "malicious"]) == 2
