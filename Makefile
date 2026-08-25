VENV ?= .venv
ifeq ($(OS),Windows_NT)
VENV_BIN := $(VENV)/Scripts
TEMP_DIR ?= $(or $(TEMP),$(TMP),.)
else
VENV_BIN := $(VENV)/bin
TEMP_DIR ?= $(or $(TMPDIR),/tmp)
endif

PYTHON := $(VENV_BIN)/python
PYTEST := $(VENV_BIN)/pytest
MYPY := $(VENV_BIN)/mypy
RUFF := $(VENV_BIN)/ruff

SQL_DATABASE ?= $(TEMP_DIR)/algo-sql-smoke.db
SQL_VALIDATION_OUT ?= $(TEMP_DIR)/algo-sql-validation.json
SQL_COMPARISON_CSV ?= $(TEMP_DIR)/algo-sql-comparison.csv
SQL_COMPARISON_METADATA ?= $(TEMP_DIR)/algo-sql-comparison.json
SQL_BENCHMARK_DATABASE ?= $(TEMP_DIR)/algo-sql-smoke.db
SQL_BENCHMARK_REPORT ?= $(TEMP_DIR)/algo-sql-smoke.json
DATA_ARTIFACT_DIR ?= artifacts/data-demo
DATA_ACQUISITION_ID ?=
CLOUD_IMAGE ?= algo-backtester-aws:local

.PHONY: test lint verify-warnings serve dashboard report install sql-validate sql-compare sql-benchmark-smoke data-acquire-demo data-inspect-demo cloud-test cloud-smoke cloud-verify cloud-container-smoke

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
		-W error::starlette.exceptions.StarletteDeprecationWarning \
		-W error::ResourceWarning \
		-W error::pytest.PytestUnraisableExceptionWarning
	uv run --extra dev pytest tests/test_db.py tests/sql_analytics \
		-W error::DeprecationWarning \
		-W error::ResourceWarning \
		-W error::pytest.PytestUnraisableExceptionWarning

serve:
	$(PYTHON) -m uvicorn src.api.main:app --reload --port 8000

dashboard:
	$(VENV_BIN)/streamlit run src/dashboard/app.py

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

cloud-test:
	uv run --frozen --extra dev --extra cloud pytest tests/cloud/test_packaging.py -q

cloud-smoke:
	uv run --frozen --extra dev --extra cloud python tests/cloud/test_packaging.py --smoke

cloud-verify: cloud-test cloud-smoke

cloud-container-smoke: cloud-verify
	@set -eu; \
		smoke_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/algo-cloud-container-smoke.XXXXXX")"; \
		container_name="algo-cloud-container-smoke-$$(date +%s)-$$$$"; \
		cleanup() { docker rm -f "$$container_name" >/dev/null 2>&1 || true; rm -rf "$$smoke_dir"; }; \
		trap cleanup EXIT HUP INT TERM; \
		docker build --platform linux/amd64 -t $(CLOUD_IMAGE) .; \
		image_id="$$(docker image inspect $(CLOUD_IMAGE) --format '{{.Id}}')"; \
		container_id="$$(docker run --detach --name "$$container_name" --platform linux/amd64 --network none --read-only --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m --mount type=bind,src="$$(pwd)/tests/cloud/test_packaging.py",dst=/harness/test_packaging.py,readonly --mount type=bind,src="$$(pwd)/tests/cloud/fixtures/spy-daily.parquet",dst=/harness/fixtures/spy-daily.parquet,readonly --entrypoint /opt/venv/bin/python -e PYTHONPATH=/harness:/app $(CLOUD_IMAGE) /harness/test_packaging.py --container-smoke --output-directory /tmp/artifacts)"; \
		echo "image_id=$$image_id container_name=$$container_name container_id=$$container_id"; \
		ready=0; attempts=0; \
		while [ "$$attempts" -lt 60 ]; do \
			if docker logs "$$container_name" 2>&1 | grep -q '"ready_for_copy": true'; then ready=1; break; fi; \
			if [ "$$(docker inspect "$$container_name" --format '{{.State.Running}}')" != true ]; then docker logs "$$container_name"; exit 1; fi; \
			attempts=$$((attempts + 1)); sleep 1; \
		done; \
		test "$$ready" = 1; \
		docker logs "$$container_name"; \
		docker exec "$$container_name" /opt/venv/bin/python -c 'import sys, tarfile; archive = tarfile.open(fileobj=sys.stdout.buffer, mode="w|"); archive.add("/tmp/artifacts", arcname="artifacts"); archive.close()' > "$$smoke_dir/artifacts.tar"; \
		tar -C "$$smoke_dir" -xf "$$smoke_dir/artifacts.tar"; \
		rm -f "$$smoke_dir/artifacts.tar"; \
		docker exec "$$container_name" /opt/venv/bin/python -c 'from pathlib import Path; Path("/tmp/release").touch()'; \
		test "$$(docker wait "$$container_name")" = 0; \
		docker logs "$$container_name"; \
		uv run --frozen --extra dev --extra cloud python tests/cloud/test_packaging.py --verify-container-artifacts "$$smoke_dir/artifacts"
