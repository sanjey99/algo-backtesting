VENV := venv/Scripts
PYTHON := $(VENV)/python
PYTEST := $(VENV)/pytest
MYPY := $(VENV)/mypy
RUFF := $(VENV)/ruff

.PHONY: test lint serve dashboard report install

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
