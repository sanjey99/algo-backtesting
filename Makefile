VENV := venv/Scripts
PYTHON := $(VENV)/python
PYTEST := $(VENV)/pytest
MYPY := $(VENV)/mypy
RUFF := $(VENV)/ruff

.PHONY: test lint serve dashboard report install data-acquire-demo data-inspect-demo

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

DATA_ARTIFACT_DIR ?= artifacts/data-demo
DATA_ACQUISITION_ID ?=

data-acquire-demo:
	$(PYTHON) -m src.data.cli acquire --symbol SPY --start 2024-01-02 --end 2024-01-10 --source auto --calendar XNYS --canonical $(DATA_ARTIFACT_DIR)/spy-bars.parquet --report $(DATA_ARTIFACT_DIR)/spy-acquisition.json --cache-dir $(DATA_ARTIFACT_DIR)/cache --manifest-dir $(DATA_ARTIFACT_DIR)/reports

data-inspect-demo:
	$(PYTHON) -m src.data.cli inspect --acquisition-id $(DATA_ACQUISITION_ID) --cache-dir $(DATA_ARTIFACT_DIR)/cache --manifest-dir $(DATA_ARTIFACT_DIR)/reports
