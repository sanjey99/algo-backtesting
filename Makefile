VENV := venv/Scripts
PYTHON := $(VENV)/python
PYTEST := $(VENV)/pytest
MYPY := $(VENV)/mypy
RUFF := $(VENV)/ruff

SQL_DATABASE ?= /private/tmp/algo-sql-smoke.db
SQL_VALIDATION_OUT ?= /private/tmp/algo-sql-validation.json
SQL_COMPARISON_CSV ?= /private/tmp/algo-sql-comparison.csv
SQL_COMPARISON_METADATA ?= /private/tmp/algo-sql-comparison.json
SQL_BENCHMARK_DATABASE ?= /private/tmp/algo-sql-smoke.db
SQL_BENCHMARK_REPORT ?= /private/tmp/algo-sql-smoke.json
DATA_ARTIFACT_DIR ?= artifacts/data-demo
DATA_ACQUISITION_ID ?=

.PHONY: test lint verify-warnings serve dashboard report install sql-validate sql-compare sql-benchmark-smoke data-acquire-demo data-inspect-demo

install:
	$(PYTHON) -m pip install -e ".[dev]" -q

test:
	$(PYTEST) tests/ --cov=src --cov-report=term-missing -q

test-verbose:
	$(PYTEST) tests/ -v

lint:
	$(RUFF) check src/ tests/
	$(MYPY) src/ --strict

verify-warnings:
	uv run --extra dev pytest tests/test_api.py tests/test_api_data_acquisition.py \
		-W error::starlette.exceptions.StarletteDeprecationWarning
	uv run --extra dev pytest tests/test_db.py tests/sql_analytics \
		-W error::DeprecationWarning

serve:
	$(PYTHON) -m uvicorn src.api.main:app --reload --port 8000

dashboard:
	$(VENV)/streamlit run src/dashboard/app.py

report:
	$(PYTHON) -m src.analytics.report --symbol SPY --strategy ma_crossover --out report.html

report-symbol:
	$(PYTHON) -m src.analytics.report --symbol $(SYMBOL) --strategy $(STRATEGY) --start $(START) --end $(END) --out report.html

sql-validate:
	$(PYTHON) -m src.analytics.sql_cli validate --database "$(SQL_DATABASE)" --out "$(SQL_VALIDATION_OUT)"

sql-compare:
	@test -n "$(SQL_SYMBOL)" || (echo "SQL_SYMBOL is required" >&2; exit 2)
	@test -n "$(SQL_START)" || (echo "SQL_START is required" >&2; exit 2)
	@test -n "$(SQL_END)" || (echo "SQL_END is required" >&2; exit 2)
	$(PYTHON) -m src.analytics.sql_cli compare --database "$(SQL_DATABASE)" --symbol "$(SQL_SYMBOL)" --start "$(SQL_START)" --end "$(SQL_END)" --csv "$(SQL_COMPARISON_CSV)" --metadata "$(SQL_COMPARISON_METADATA)"

sql-benchmark-smoke:
	$(PYTHON) -m src.analytics.sql_cli benchmark --runs 6 --equity-points-per-run 20 --trades-per-run 4 --warmups 1 --repetitions 3 --database-out "$(SQL_BENCHMARK_DATABASE)" --out "$(SQL_BENCHMARK_REPORT)"

data-acquire-demo:
	$(PYTHON) -m src.data.cli acquire --symbol SPY --start 2024-01-02 --end 2024-01-10 --source auto --calendar XNYS --canonical $(DATA_ARTIFACT_DIR)/spy-bars.parquet --report $(DATA_ARTIFACT_DIR)/spy-acquisition.json --cache-dir $(DATA_ARTIFACT_DIR)/cache --manifest-dir $(DATA_ARTIFACT_DIR)/reports

data-inspect-demo:
	$(PYTHON) -m src.data.cli inspect --acquisition-id $(DATA_ACQUISITION_ID) --cache-dir $(DATA_ARTIFACT_DIR)/cache --manifest-dir $(DATA_ARTIFACT_DIR)/reports
