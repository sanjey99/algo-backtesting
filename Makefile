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

.PHONY: test lint serve dashboard report install sql-validate sql-compare sql-benchmark-smoke

install:
	$(PYTHON) -m pip install -e ".[dev]" -q

test:
	$(PYTEST) tests/ --cov=src --cov-report=term-missing -q

test-verbose:
	$(PYTEST) tests/ -v

lint:
	$(RUFF) check src/ tests/
	$(MYPY) src/ --strict

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
