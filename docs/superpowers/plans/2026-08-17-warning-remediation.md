# Compatibility Warning Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the known Starlette TestClient and SQLAlchemy UTC deprecation warnings through precise compatibility changes.

**Architecture:** Use Starlette's supported `httpx2` TestClient transport without changing FastAPI test call sites, and replace the application-owned `datetime.utcnow` SQLAlchemy default with an explicit naive-UTC helper. Lockfile changes remain isolated from execution behavior.

**Tech Stack:** uv, FastAPI 0.141.1, Starlette 1.3.1, httpx/httpx2, SQLAlchemy 2.0.51, pytest warning filters.

**Spec:** `docs/superpowers/specs/2026-08-17-execution-realism-design.md`

## Global Constraints

- Keep SQLite `DateTime` values timezone-naive but define them as UTC by contract.
- No Alembic migration: the physical column type does not change.
- Retain imports from `fastapi.testclient.TestClient`.
- Remove direct `httpx` only after verifying `src` and `tests` have no direct imports.
- Preserve `.codegraph/` and do not push.

## File Structure

- Modify `src/db/tables.py`: application-owned naive UTC default helper.
- Modify `tests/test_db.py`: warning-as-error/default semantics test.
- Modify `pyproject.toml` and `uv.lock`: replace unused direct `httpx` with development `httpx2`.
- Modify API tests only if transport behavior—not imports—requires compatibility adjustments.

---

### Task 1: Replace deprecated UTC default

**Files:**
- Modify: `src/db/tables.py:1-30`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `_utc_now_naive() -> datetime` for `BacktestRun.created_at`.

- [ ] **Step 1: Write the failing warning-as-error test**

```python
def test_created_at_default_is_naive_utc_without_deprecation(db_session: Session) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        run_id = save_backtest_run(db_session, **SAMPLE_RUN)
        db_session.commit()
    created_at = get_backtest_run(db_session, run_id).created_at
    assert created_at.tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - created_at).total_seconds()) < 5
```

- [ ] **Step 2: Run and confirm RED**

Run: `uv run --extra dev pytest tests/test_db.py::test_created_at_default_is_naive_utc_without_deprecation -q`

Expected: `datetime.utcnow()` raises `DeprecationWarning` on Python 3.13.

- [ ] **Step 3: Implement the explicit naive UTC helper**

```python
from datetime import UTC, datetime

def _utc_now_naive() -> datetime:
    """Return UTC for SQLite's intentionally timezone-naive DateTime contract."""
    return datetime.now(UTC).replace(tzinfo=None)
```

Set `created_at` to `mapped_column(DateTime, default=_utc_now_naive, nullable=False)`.

- [ ] **Step 4: Run DB and SQL analytics tests**

Run: `uv run --extra dev pytest tests/test_db.py tests/sql_analytics -W error::DeprecationWarning -q`

Expected: PASS without UTC warnings.

- [ ] **Step 5: Commit the UTC fix**

```bash
git add src/db/tables.py tests/test_db.py
git commit -m "fix: use explicit UTC database defaults"
```

### Task 2: Migrate Starlette TestClient transport to httpx2

**Files:**
- Modify: `pyproject.toml:8-39`
- Modify: `uv.lock`
- Test: `tests/test_api.py`
- Test: `tests/test_api_data_acquisition.py`

**Interfaces:**
- Preserves: `from fastapi.testclient import TestClient` and existing synchronous test methods.
- Produces: development dependency on Starlette's supported `httpx2` transport.

- [ ] **Step 1: Prove there are no direct httpx imports**

Run: `rg -n '(^|\\s)(from|import) httpx' src tests`

Expected: no matches. If a match exists, keep direct `httpx` and add `httpx2` rather than removing it.

- [ ] **Step 2: Capture the current warning as a failing gate**

Run: `uv run --extra dev pytest tests/test_api.py::TestHealth::test_health_ok -W error::starlette.exceptions.StarletteDeprecationWarning -q`

Expected: FAIL with the plain-httpx TestClient deprecation.

- [ ] **Step 3: Update dependencies with uv**

Run: `uv remove httpx`

Run: `uv add --optional dev httpx2`

Review `pyproject.toml` to confirm `httpx2` is in `[project.optional-dependencies].dev` and inspect
`uv lock --check` plus the lockfile diff for unrelated version churn.

- [ ] **Step 4: Run API tests with the warning promoted to error**

Run: `uv run --extra dev pytest tests/test_api.py tests/test_api_data_acquisition.py -W error::starlette.exceptions.StarletteDeprecationWarning -q`

Expected: PASS with existing FastAPI `TestClient` imports.

- [ ] **Step 5: Commit transport compatibility**

```bash
git add pyproject.toml uv.lock tests/test_api.py tests/test_api_data_acquisition.py
git commit -m "chore: migrate tests to Starlette httpx2 transport"
```

### Task 3: Make warning cleanliness a release gate

**Files:**
- Modify: `Makefile`
- Modify: `README.md`

**Interfaces:**
- Produces: documented `verify-warnings` command with the two remediated categories treated as errors.

- [ ] **Step 1: Add the Make target**

```make
.PHONY: verify-warnings
verify-warnings:
	uv run --extra dev pytest tests/test_api.py tests/test_api_data_acquisition.py \
		-W error::starlette.exceptions.StarletteDeprecationWarning
	uv run --extra dev pytest tests/test_db.py tests/sql_analytics \
		-W error::DeprecationWarning
```

- [ ] **Step 2: Document the compatibility gate**

Add `make verify-warnings` beside the existing test/lint/type verification commands and explain that
it guards Starlette transport and naive-UTC persistence compatibility.

- [ ] **Step 3: Run final warning and project verification**

Run: `make verify-warnings`

Run: `uv run --extra dev pytest`

Run: `uv run --extra dev pytest --cov=src --cov-report=term-missing --cov-fail-under=80`

Run: `uv run --extra dev ruff check src tests`

Run: `uv run --extra dev mypy src --strict`

Expected: all tests pass, source coverage is at least 80%, both targeted warning classes are absent,
and lint/type checks pass.

- [ ] **Step 4: Commit the verification gate**

```bash
git add Makefile README.md
git commit -m "ci: enforce compatibility warning gates"
```
