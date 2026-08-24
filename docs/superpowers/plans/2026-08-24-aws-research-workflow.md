# AWS Research Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure, cost-bounded AWS batch research workflow that reuses the existing acquisition, backtest, analytics, and report code while keeping the current local FastAPI, SQLite, and dashboard paths unchanged.

**Architecture:** New `src/cloud` adapters admit a closed workflow contract, publish immutable S3 artifacts, persist workflow state in DynamoDB, and run the existing engine in a one-shot Fargate task orchestrated by Step Functions. A separate GET-only API Gateway path exposes finalized public summaries and five-minute report links; no public route can start paid compute.

**Tech Stack:** Python 3.12, Pydantic 2, boto3, pandas/PyArrow, pytest, Ruff, strict mypy, Docker, AWS Lambda container images, ECS Fargate, Step Functions Standard, S3, DynamoDB, ECR, API Gateway HTTP API, CloudWatch, EventBridge, AWS Budgets, Terraform 1.10+, TFLint, Checkov, GitHub Actions OIDC.

**Spec:** `docs/superpowers/specs/2026-08-24-aws-research-workflow-design.md`

## Global Constraints

- Work only on branch `codex/aws-research-workflow` in `/Users/sanje/code/algo-backtesting`.
- Preserve the user's untracked `.codegraph/` directory and never stage it.
- Follow strict RED → confirm failure → GREEN → confirm pass for every Python behavior.
- Do not change the semantics of `src/data/store.py`, `src/api/jobs.py`, the local SQLite path, the dashboard, or the existing API.
- Keep AWS clients behind injected protocols. Unit and integration tests must run without credentials and without network access.
- Reject unknown fields, non-finite numbers, malformed UUIDs, unsafe object keys, and caller-selected output locations at admission.
- Use derived S3 keys. Callers may supply neither a bucket nor any key or prefix.
- Keep the bucket private. Public access is only through `GET /runs/{run_id}` and a five-minute presigned report URL.
- A run becomes `SUCCEEDED` only after the finalizer verifies the exact artifact set, byte lengths, and SHA-256 digests.
- Use DynamoDB conditional updates for every state transition and make terminal replay idempotent.
- Do not create or mutate live AWS resources in this implementation phase. Deployment requires a separate approval after `terraform plan`, budget-recipient review, promotional-credit applicability review, and cleanup review.
- Never commit credentials, account IDs, Terraform state, provider payloads, raw market bars, or generated result artifacts.
- Keep at least the repository's current 80% coverage gate; the baseline before this plan was 753 passing tests and 91% coverage.

---

## Task 1: Add cloud dependencies and closed contracts

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/cloud/__init__.py`
- Create: `src/cloud/contracts.py`
- Create: `tests/cloud/__init__.py`
- Create: `tests/cloud/test_contracts.py`

**Interfaces:**

- `ResearchRequest.model_validate(value: object) -> ResearchRequest`
- `DatasetRef.model_validate(value: object) -> DatasetRef`
- `RunSpec.create(...) -> RunSpec`
- `ArtifactDigest.model_validate(value: object) -> ArtifactDigest`
- `ChecksumsManifest.model_validate(value: object) -> ChecksumsManifest`
- `RunRecord.model_validate(value: object) -> RunRecord`
- `canonical_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes`
- `sha256_hex(value: bytes) -> str`

- [ ] Add the cloud runtime dependencies under a dedicated extra so the ordinary local install does not gain an implicit AWS requirement:

  ```toml
  [project.optional-dependencies]
  cloud = [
      "awslambdaric>=3.0,<4",
      "boto3>=1.35,<2",
  ]
  ```

  Add `PyYAML>=6,<7` to the existing `dev` extra for structural workflow tests; use `yaml.BaseLoader` so the YAML 1.1 parser does not coerce the GitHub Actions `on` key into a boolean.

- [ ] Run `uv lock` and confirm `uv sync --locked --extra dev --extra cloud` succeeds.
- [ ] Write `tests/cloud/test_contracts.py` first. Cover all three registered strategies, unknown strategy/parameter rejection, unknown field rejection, invalid dates, reversed ranges, non-finite numbers, invalid image digests, invalid UUIDs, unsafe keys, caller-supplied output prefixes, UTC timestamps, and deterministic JSON bytes. Include this representative failure:

  ```python
  def test_research_request_rejects_output_location_and_unknown_fields() -> None:
      with pytest.raises(ValidationError):
          ResearchRequest.model_validate(
              {
                  "symbol": "SPY",
                  "start": "2024-01-02",
                  "end": "2024-03-28",
                  "strategy_key": "ma_crossover",
                  "strategy_parameters": {"fast_period": 10, "slow_period": 50},
                  "output_prefix": "runs/v1/chosen-by-caller/",
              }
          )
  ```

- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_contracts.py -q` and confirm collection fails because `src.cloud.contracts` does not exist.
- [ ] Implement immutable Pydantic contracts with `ConfigDict(extra="forbid", frozen=True, strict=True)`. Use these closed enums and constants:

  ```python
  SCHEMA_VERSION = "1"
  REQUIRED_ARTIFACTS = frozenset(
      {
          "run-spec.json",
          "result.json",
          "trades.parquet",
          "equity-curve.parquet",
          "report.html",
      }
  )

  class Visibility(StrEnum):
      PRIVATE = "PRIVATE"
      PUBLIC = "PUBLIC"

  class RunStatus(StrEnum):
      PENDING = "PENDING"
      RUNNING = "RUNNING"
      SUCCEEDED = "SUCCEEDED"
      FAILED = "FAILED"

  class FailureCode(StrEnum):
      ACQUISITION_FAILED = "ACQUISITION_FAILED"
      PREPARATION_FAILED = "PREPARATION_FAILED"
      WORKER_FAILED = "WORKER_FAILED"
      ARTIFACT_VERIFICATION_FAILED = "ARTIFACT_VERIFICATION_FAILED"
      WORKFLOW_TIMED_OUT = "WORKFLOW_TIMED_OUT"
  ```

- [ ] Implement `ResearchRequest` with symbol/date validation compatible with `AcquisitionRequest`, an inclusive range capped at ten years, `strategy_key` membership in `STRATEGY_REGISTRY`, at most 16 finite scalar strategy parameters, positive finite capital, nonnegative finite commission/slippage, and visibility defaulting to `PRIVATE`. Parse ISO date strings explicitly before strict validation. Instantiate the selected strategy during admission so unsupported parameters fail before any paid task starts.
- [ ] Implement strict object-key validation with maximum length 1,024, required `datasets/v1/` or `runs/v1/` prefixes, no empty/dot components, no backslash, and no control characters. Validate 64-character lowercase hexadecimal SHA-256 values and `sha256:` image digests.
- [ ] Implement `RunSpec.create()` so it accepts an admitted `ResearchRequest`, pinned `DatasetRef`, trusted image digest, injected UTC clock, and injected UUID factory, then derives `run_spec_key` and `result_prefix` internally:

  ```python
  @classmethod
  def create(
      cls,
      *,
      request: ResearchRequest,
      dataset: DatasetRef,
      image_digest: str,
      now: datetime,
      run_id: UUID,
      maximum_runtime_seconds: int = 600,
  ) -> RunSpec:
      run_id_text = str(run_id)
      return cls(
          run_id=run_id_text,
          dataset=dataset,
          request=request,
          image_digest=image_digest,
          created_at=now,
          maximum_runtime_seconds=maximum_runtime_seconds,
          run_spec_key=f"runs/v1/{run_id_text}/run-spec.json",
          result_prefix=f"runs/v1/{run_id_text}/",
      )
  ```

- [ ] Make `canonical_json_bytes()` use sorted keys, compact separators, UTF-8, and `allow_nan=False`. This intentionally turns the engine's possible infinite `profit_factor` into `null` at the result-mapping boundary rather than emitting non-standard JSON.
- [ ] Run the contract test again and confirm it passes.
- [ ] Run `uv run --extra dev --extra cloud ruff check src/cloud tests/cloud` and `uv run --extra dev --extra cloud mypy src/cloud --strict`.
- [ ] Commit only Task 1 files:

  ```text
  feat: define AWS research contracts
  ```

---

## Task 2: Build injectable S3 and DynamoDB boundaries

**Files:**

- Create: `src/cloud/storage.py`
- Create: `tests/cloud/fakes.py`
- Create: `tests/cloud/test_storage.py`

**Interfaces:**

- `ObjectStore.put(key: str, body: bytes, content_type: str) -> StoredObject`
- `ObjectStore.get(key: str, maximum_bytes: int) -> bytes`
- `ObjectStore.head(key: str) -> StoredObject`
- `ObjectStore.presign_get(key: str, expires_seconds: int) -> str`
- `RunRepository.create_pending(record: RunRecord) -> None`
- `RunRepository.mark_running(run_id: str, started_at: datetime) -> None`
- `RunRepository.mark_succeeded(run_id: str, completed_at: datetime) -> None`
- `RunRepository.mark_failed(run_id: str, code: FailureCode, completed_at: datetime) -> None`
- `RunRepository.get(run_id: str, consistent: bool = False) -> RunRecord | None`
- `S3ObjectStore` and `DynamoRunRepository` production implementations

- [ ] Write in-memory fakes that make immutable copies on every call and expose call records without leaking mutable internal state:

  ```python
  @dataclass(frozen=True, slots=True)
  class PutCall:
      key: str
      body: bytes
      content_type: str

  class FakeObjectStore:
      def __init__(self) -> None:
          self._objects: dict[str, bytes] = {}
          self._content_types: dict[str, str] = {}
          self._put_calls: list[PutCall] = []

      @property
      def put_calls(self) -> tuple[PutCall, ...]:
          return tuple(self._put_calls)
  ```

- [ ] Write `tests/cloud/test_storage.py` first with recording boto clients. Assert exact bucket/key, explicit `ServerSideEncryption="AES256"`, SHA-256 metadata, content type, body size limits, five-minute presign ceiling, consistent reads, DynamoDB expression names/values, and every allowed/forbidden transition.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_storage.py -q` and confirm failure because the storage adapters do not exist.
- [ ] Define `ObjectStore` and `RunRepository` as `Protocol` types. Define immutable `StoredObject(key, byte_length, sha256)`.
- [ ] Implement `S3ObjectStore` with an injected client and fixed bucket. It must validate every key, reject downloads whose `ContentLength` exceeds `maximum_bytes` before reading the body, verify the downloaded body length, and never accept a caller-selected bucket:

  ```python
  self._client.put_object(
      Bucket=self._bucket,
      Key=key,
      Body=body,
      ContentType=content_type,
      ServerSideEncryption="AES256",
      Metadata={"sha256": sha256_hex(body)},
      IfNoneMatch="*",
  )
  ```

  On `PreconditionFailed`, fetch the existing bounded object and accept the replay only if its bytes have the same digest; otherwise raise `ImmutableObjectConflict`.

- [ ] Implement `DynamoRunRepository` with an injected table and the following conditions:

  ```text
  create_pending: attribute_not_exists(PK)
  mark_running:   #status = :pending
  mark_succeeded: #status = :running OR (#status = :succeeded AND completed_at = :completed)
  mark_failed:    #status IN (:pending, :running) OR
                  (#status = :failed AND failure_code = :code AND completed_at = :completed)
  ```

  Map botocore conditional failures to a closed `StateTransitionError` without including raw AWS response text.
- [ ] Re-run storage tests and confirm they pass.
- [ ] Run Ruff and strict mypy for the new module and tests.
- [ ] Commit only Task 2 files:

  ```text
  feat: add cloud storage and run-state adapters
  ```

---

## Task 3: Publish pinned acquisition artifacts

**Files:**

- Create: `src/cloud/ingestion_handler.py`
- Create: `tests/cloud/test_ingestion_handler.py`

**Interfaces:**

- `acquire_dataset(request: ResearchRequest, *, service_factory, object_store, clock) -> DatasetRef`
- `handle_ingestion(event: object, *, service_factory, object_store, clock) -> dict[str, object]`
- `lambda_handler(event: object, context: object) -> dict[str, object]`

- [ ] Write handler tests first using a fake acquisition service and `FakeObjectStore`. Prove normalized Parquet and redacted manifest publication, accepted `SUCCESS` and `PARTIAL_SUCCESS`, deterministic keys containing acquisition ID plus data digest, no raw provider response publication, isolated temporary directories, and typed failure for failed acquisition.
- [ ] Include a test with secret-shaped manifest metadata and assert no secret value appears in any uploaded body.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_ingestion_handler.py -q` and confirm failure because the handler is missing.
- [ ] Implement `acquire_dataset()` by creating a fresh `TemporaryDirectory(dir="/tmp")`, passing child cache/manifest paths to `create_acquisition_service`, and calling the existing `AcquisitionService.acquire()` with an `AcquisitionRequest` fixed to source `YFINANCE`, calendar `XNYS`, interval `1d`, cache enabled, and refresh disabled. Alpha Vantage remains outside Phase 1 and no secret is required.
- [ ] Serialize the canonical frame as Parquet with `index=False`, compute the digest over the exact bytes, and derive keys without a mutable pointer:

  ```python
  dataset_key = (
      f"datasets/v1/{result.manifest.acquisition_id}/"
      f"{request.symbol}-{dataset_digest}.parquet"
  )
  manifest_key = (
      f"datasets/v1/{result.manifest.acquisition_id}/"
      f"manifest-{manifest_digest}.json"
  )
  ```

- [ ] Upload only the canonical Parquet and `result.manifest.to_dict()` canonical JSON. Use the existing redaction contract rather than serializing arbitrary exception objects.
- [ ] Return a `DatasetRef` plus the admitted request as plain JSON values for Step Functions. Keep boto3/environment assembly solely in `lambda_handler`.
- [ ] Log `cloud.acquisition.started`, `cloud.acquisition.succeeded`, and a closed failure code through `src.observability.log_event`; never log the event payload or provider exception text.
- [ ] Re-run ingestion tests and confirm they pass, then run Ruff and strict mypy.
- [ ] Commit only Task 3 files:

  ```text
  feat: publish immutable cloud datasets
  ```

---

## Task 4: Prepare immutable runs and conditional metadata

**Files:**

- Create: `src/cloud/prepare_handler.py`
- Create: `tests/cloud/test_prepare_handler.py`

**Interfaces:**

- `prepare_run(event: object, *, object_store, run_repository, image_digest, clock, uuid_factory, ttl_days) -> PreparedRun`
- `lambda_handler(event: object, context: object) -> dict[str, object]`

- [ ] Write tests first. Prove UUID injection, derived keys, pinned dataset and image digest, immutable run-spec bytes, `PENDING` creation, 45-day TTL, private-by-default visibility, and no state record when S3 publication fails.
- [ ] Add a conditional-conflict test proving a duplicate UUID does not overwrite the first run.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_prepare_handler.py -q` and confirm the missing-module failure.
- [ ] Implement `prepare_run()` so it validates the full ingestion output again, constructs `RunSpec.create()`, writes `run-spec.json` first, and only then conditionally creates the `PENDING` record. If DynamoDB creation fails, leave the immutable orphan for lifecycle cleanup and log only its derived run ID.
- [ ] Return only the data Step Functions needs to start the task and finalize it:

  ```python
  @dataclass(frozen=True, slots=True)
  class PreparedRun:
      run_id: str
      run_spec_key: str

      def to_dict(self) -> dict[str, str]:
          return {
              "run_id": self.run_id,
              "run_spec_key": self.run_spec_key,
          }
  ```

- [ ] Read `ENGINE_IMAGE_DIGEST`, `ARTIFACT_BUCKET`, `RUN_TABLE`, and `RUN_TTL_DAYS` only in `lambda_handler`. Reject a mutable image tag or absent digest at cold start.
- [ ] Re-run prepare tests and confirm they pass, then run Ruff and strict mypy.
- [ ] Commit only Task 4 files:

  ```text
  feat: prepare pinned cloud backtest runs
  ```

---

## Task 5: Execute the existing engine as a one-shot worker

**Files:**

- Create: `src/cloud/worker.py`
- Create: `tests/cloud/test_worker.py`
- Create: `tests/cloud/fixtures/spy-daily.parquet`

**Interfaces:**

- `execute_run(run_spec_key: str, *, object_store, run_repository, clock) -> ChecksumsManifest`
- `result_summary(result: BacktestResult, metrics: Mapping[str, float]) -> dict[str, object]`
- `trade_frame(result: BacktestResult) -> pd.DataFrame`
- `equity_frame(result: BacktestResult) -> pd.DataFrame`
- `main(argv: Sequence[str] | None = None) -> int`

- [ ] Generate the small deterministic Parquet fixture through the project's normal fixture tooling or a one-off test setup, inspect it, and commit it only if it contains synthetic/nonlicensed values. Record its columns and SHA-256 in the test.
- [ ] Write worker tests first. The main happy-path test must run the fixture through real `df_to_candles`, real `STRATEGY_REGISTRY`, real `BacktestEngine`, real metrics, and real standalone report code while faking only S3 and DynamoDB.
- [ ] Assert `PENDING -> RUNNING`, exact dataset checksum verification before engine invocation, exact artifact names, deterministic JSON schema, valid Parquet outputs, HTML report generation, and final `checksums.json` written last.
- [ ] Add failures for dataset digest mismatch, run-spec digest mismatch, unknown strategy, bad parameters, oversized dataset, malformed Parquet, and an already-terminal run. Assert no result artifact is written when admission fails.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_worker.py -q` and confirm failure because the worker is missing.
- [ ] Implement the worker in this order:

  ```text
  validate run-spec key -> download bounded run spec -> validate RunSpec
  -> verify run spec corresponds to its derived key -> mark RUNNING
  -> download bounded dataset -> verify SHA-256 -> pd.read_parquet
  -> df_to_candles -> instantiate registered strategy -> BacktestEngine.run
  -> compute_all_metrics -> map non-finite metrics to null
  -> generate report and Parquet artifacts -> upload artifacts
  -> upload checksums.json last -> exit zero
  ```

- [ ] Serialize public `result.json` as summary data only: run ID, schema version, symbol, date range, strategy name and admitted parameters, initial/final equity, finite metrics or `null`, total trades, image digest, dataset digest, and completion time. Do not include raw bars, trades, or equity arrays.
- [ ] Serialize `trades.parquet` with explicit columns even for zero trades, including `trade_id`, symbol, direction, entry/exit timestamps and prices, quantity, commission, PnL, and PnL percent. Serialize `equity-curve.parquet` with UTC timestamp, equity, and drawdown percent.
- [ ] Use the object store's conditional `IfNoneMatch="*"` publication for every artifact and accept only a byte-identical replay. This protects idempotent task replay from divergent output without a check-then-write race.
- [ ] `main()` must accept exactly `--run-spec-key`, construct fixed-bucket clients from environment, configure JSON logging, return `0` only after checksums publication, and return a nonzero code after logging a closed failure event.
- [ ] Re-run worker tests and confirm they pass. Run the focused test twice to demonstrate deterministic schemas and exact artifact sets.
- [ ] Run Ruff and strict mypy.
- [ ] Commit only Task 5 files:

  ```text
  feat: run cloud backtests in a batch worker
  ```

---

## Task 6: Verify artifacts before terminal state

**Files:**

- Create: `src/cloud/finalize_handler.py`
- Create: `tests/cloud/test_finalize_handler.py`

**Interfaces:**

- `finalize_success(run_id: str, *, object_store, run_repository, clock) -> RunRecord`
- `finalize_failure(run_id: str, code: FailureCode, *, run_repository, clock) -> RunRecord`
- `handle_finalization(event: object, *, object_store, run_repository, clock) -> dict[str, object]`
- `lambda_handler(event: object, context: object) -> dict[str, object]`

- [ ] Write finalizer tests first for valid artifacts, missing artifact, extra artifact, duplicate name, oversized artifact, wrong length, wrong digest, invalid checksum manifest, result/run ID mismatch, success replay, failure from `PENDING`, failure from `RUNNING`, and conflicting terminal replay.
- [ ] Assert `mark_succeeded` is never called until every required artifact has been downloaded within its content-specific cap and verified.
- [ ] Use these explicit maximum sizes in tests and implementation:

  ```python
  ARTIFACT_MAXIMUM_BYTES = {
      "run-spec.json": 64 * 1024,
      "result.json": 64 * 1024,
      "trades.parquet": 16 * 1024 * 1024,
      "equity-curve.parquet": 32 * 1024 * 1024,
      "report.html": 8 * 1024 * 1024,
      "checksums.json": 32 * 1024,
  }
  ```

- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_finalize_handler.py -q` and confirm the missing-module failure.
- [ ] Implement success finalization by deriving `runs/v1/{run_id}/checksums.json`, requiring the exact `REQUIRED_ARTIFACTS` set, verifying lengths and hashes, parsing `result.json`, and checking its run ID before the conditional `RUNNING -> SUCCEEDED` update.
- [ ] Implement failure finalization with only a `FailureCode`; do not accept arbitrary error strings or AWS `Cause` objects. Map Step Functions branches to a closed code before invoking the handler.
- [ ] Return the canonical run record, not the event. Log detailed exception types internally through the existing redacted logger and expose only closed codes.
- [ ] Re-run finalizer tests and confirm they pass, then run Ruff and strict mypy.
- [ ] Commit only Task 6 files:

  ```text
  feat: verify cloud artifacts before finalization
  ```

---

## Task 7: Expose finalized public summaries through a GET-only handler

**Files:**

- Create: `src/cloud/results_handler.py`
- Create: `tests/cloud/test_results_handler.py`

**Interfaces:**

- `get_public_result(run_id: str, *, object_store, run_repository) -> ApiResponse`
- `lambda_handler(event: object, context: object) -> dict[str, object]`

- [ ] Write results tests first for canonical UUID parsing, absent/private/pending/running/failed records, malformed records, missing final manifest, tampered result, oversized result, successful public result, five-minute presign, response-size cap, and CORS/method behavior.
- [ ] Assert every hidden or invalid case produces the byte-identical response below, preventing status/visibility enumeration:

  ```python
  NOT_FOUND = ApiResponse(
      status_code=404,
      headers={"content-type": "application/json", "cache-control": "no-store"},
      body=b'{"error":"run_not_found"}',
  )
  ```

- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_results_handler.py -q` and confirm the missing-module failure.
- [ ] Implement a v2 HTTP API event adapter that accepts only `GET` and exactly one `run_id` path parameter. Do not process query strings, request bodies, caller buckets, or caller keys.
- [ ] For a candidate record, require `SUCCEEDED`, `PUBLIC`, and no failure code. Derive both `checksums.json` and `report.html` keys from the admitted UUID, verify the result/checksum relationship again, and generate a 300-second presigned URL.
- [ ] Return only the bounded `result.json` summary plus `report_url` and `report_url_expires_seconds`. Cap the serialized response at 64 KiB and use `cache-control: no-store` because the signed URL expires.
- [ ] Do not include permissive wildcard CORS headers in Phase 1. API Gateway should reject all methods for which no route exists.
- [ ] Re-run result tests and confirm they pass, then run Ruff and strict mypy.
- [ ] Commit only Task 7 files:

  ```text
  feat: expose finalized public research results
  ```

---

## Task 8: Package one non-root image for Lambda and Fargate

**Files:**

- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `tests/cloud/test_packaging.py`
- Modify: `Makefile`

**Interfaces:**

- Image default entrypoint: `/usr/local/bin/python -m awslambdaric`
- Lambda command override: one of the `src.cloud.*_handler.lambda_handler` strings
- ECS command override: `python -m src.cloud.worker --run-spec-key runs/v1/{uuid}/run-spec.json`
- Make targets: `cloud-test`, `cloud-smoke`, `cloud-verify`

- [ ] Write packaging tests first that parse the Dockerfile and `.dockerignore`. Assert Python 3.12, locked cloud install, no copied `.git`, `.env`, `.codegraph`, data caches, Terraform state, or test artifacts, no exposed port, a numeric non-root user, and an explicit Runtime Interface Client entrypoint.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_packaging.py -q` and confirm failure because the container files do not exist.
- [ ] Implement a multi-stage image. The build stage installs exactly the lockfile's production plus cloud dependencies; the runtime stage copies only the virtual environment, `src`, and required packaged assets. Use a fixed numeric UID/GID and `/tmp` as the only writable path:

  ```dockerfile
  FROM python:3.12-slim AS runtime
  ENV PATH="/opt/venv/bin:$PATH" \
      PYTHONDONTWRITEBYTECODE=1 \
      PYTHONUNBUFFERED=1 \
      HOME=/tmp
  RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app
  COPY --from=build /opt/venv /opt/venv
  COPY --chown=10001:10001 src /app/src
  WORKDIR /app
  USER 10001:10001
  ENTRYPOINT ["/opt/venv/bin/python", "-m", "awslambdaric"]
  CMD ["src.cloud.results_handler.lambda_handler"]
  ```

- [ ] Add Make targets using `uv run --extra dev --extra cloud` for focused tests and local fixture smoke. The smoke target must use an in-memory/local object-store harness and must not require AWS credentials.
- [ ] Re-run packaging tests and confirm they pass.
- [ ] Run `docker build --platform linux/amd64 -t algo-backtester-aws:local .`.
- [ ] Run the container as its declared user against the offline fixture smoke path and verify all six run artifacts are produced beneath a temporary mounted directory. Do not publish the image.
- [ ] Run `docker inspect algo-backtester-aws:local` and confirm no port, no secrets, and numeric non-root user.
- [ ] Commit only Task 8 files:

  ```text
  build: package the AWS research runtime
  ```

---

## Task 9: Define Terraform bootstrap and cost-safe foundations

**Files:**

- Create: `infra/bootstrap/versions.tf`
- Create: `infra/bootstrap/providers.tf`
- Create: `infra/bootstrap/variables.tf`
- Create: `infra/bootstrap/main.tf`
- Create: `infra/bootstrap/outputs.tf`
- Create: `infra/versions.tf`
- Create: `infra/providers.tf`
- Create: `infra/variables.tf`
- Create: `infra/locals.tf`
- Create: `infra/storage.tf`
- Create: `infra/dynamodb.tf`
- Create: `infra/ecr.tf`
- Create: `infra/network.tf`
- Create: `infra/tests/safe_defaults.tftest.hcl`
- Create: `.tflint.hcl`
- Create: `.checkov.yml`

**Interfaces:**

- Bootstrap outputs: `state_bucket_name`, `backend_region`
- Application variables: project/environment/owner/cost-center/expiry, region, alert emails, image digest, GitHub repository, deploy ref/environment, `enable_schedule`
- Foundation outputs: artifact bucket, run table, ECR repository, VPC/subnet/security-group IDs

- [ ] Write `infra/tests/safe_defaults.tftest.hcl` first with a mocked AWS provider. Assert `enable_schedule == false`, region `ap-southeast-1`, DynamoDB billing mode `PAY_PER_REQUEST`, TTL enabled, S3 public-access blocks true, ECR max three images, no ingress rules, and no NAT gateway, load balancer, RDS, EFS, or ECS service resources.
- [ ] Run `terraform -chdir=infra init -backend=false` then `terraform -chdir=infra test` and confirm failure because the root configuration does not exist.
- [ ] Implement the bootstrap stack as a separate apply boundary: private versioned SSE-S3 state bucket, public access block, bucket-owner enforcement, and native S3 lockfile compatibility. Do not create credentials or hardcode a backend bucket in the application stack.
- [ ] Implement validated variables. Require nonempty tags, ISO expiry date, nonempty budget email list, repository grammar `owner/name`, lowercase 64-hex image digest, and default `enable_schedule = false`.
- [ ] Build immutable default tags in `locals.tf` and apply them to every taggable resource:

  ```hcl
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      Owner       = var.owner
      CostCenter  = var.cost_center
      ExpiresOn   = var.expiry_date
      ManagedBy   = "terraform"
    }
  }
  ```

- [ ] Implement one private/versioned/SSE-S3 artifact bucket with object ownership and lifecycle: abort incomplete multipart uploads after one day, private/dataset objects after 45 days, and tagged selected-public objects after 90 days. Keep every public-access block enabled.
- [ ] Implement an on-demand DynamoDB table keyed by `PK`, TTL attribute `expires_at`, SSE enabled, point-in-time recovery disabled for the bounded demo, and deletion protection disabled so cleanup remains possible.
- [ ] Implement private ECR with scan-on-push, mutable tags allowed only for build convenience, execution pinned by digest, and lifecycle retaining at most three tagged images while expiring untagged images after one day.
- [ ] Implement one VPC, one public subnet, Internet Gateway, route table, and task security group with no ingress and TCP 443 egress. Do not add NAT, IPv6, or other egress.
- [ ] Run `terraform fmt -recursive`, `terraform -chdir=infra validate`, `terraform -chdir=infra test`, `tflint --chdir=infra --recursive`, and `checkov -d infra`. Resolve every high-severity result, document only narrowly justified suppressions inline, and confirm every command passes.
- [ ] Commit only Task 9 files:

  ```text
  feat: define cost-safe AWS foundations
  ```

---

## Task 10: Define least-privilege runtime, orchestration, API, and alarms

**Files:**

- Create: `infra/iam.tf`
- Create: `infra/lambda.tf`
- Create: `infra/ecs.tf`
- Create: `infra/step_functions.tf`
- Create: `infra/eventbridge.tf`
- Create: `infra/api_gateway.tf`
- Create: `infra/cloudwatch.tf`
- Create: `infra/budgets.tf`
- Create: `infra/outputs.tf`
- Modify: `infra/tests/safe_defaults.tftest.hcl`
- Create: `tests/cloud/test_infrastructure_contract.py`

**Interfaces:**

- Lambda commands: ingestion, preparation, finalization, results
- Step Functions input: exact `ResearchRequest` JSON
- State-machine output: `run_id` and terminal status
- HTTP route: only `GET /runs/{run_id}`
- Outputs: state-machine ARN, public API base URL, cluster/task identifiers, log groups, budget name, schedule-enabled flag

- [ ] Extend the Terraform test first. Assert four Lambda functions, one ECS task definition and no service, 0.5 vCPU/1 GB task sizing, fixed image digest, timeouts, results reserved concurrency two, one GET route, route throttle 2/burst 5, 14-day logs, disabled schedule, actual/forecast budget notifications, and a 15-minute Step Functions timeout.
- [ ] Write `tests/cloud/test_infrastructure_contract.py` first to inspect the rendered state-machine definition and IAM JSON. Assert one `.sync` Fargate task, fixed task definition, one task, command override containing only the derived run-spec key, bounded retry classes, catch-to-closed-failure mapping, no raw error payload persisted, no public start route, no wildcard data-plane actions, and no dataset access in the results role.
- [ ] Run the focused Python and Terraform tests and confirm they fail because runtime infrastructure is absent.
- [ ] Implement distinct IAM roles/policies for acquisition Lambda, preparation Lambda, Fargate execution, Fargate task, finalizer Lambda, results Lambda, Step Functions, and disabled EventBridge scheduling. Scope S3 resources by role-specific prefix and DynamoDB permissions to the one run table; use `dynamodb:LeadingKeys` where supported.
- [ ] Create four Lambda container functions from the same ECR digest with handler-specific commands and read-only filesystems except `/tmp`. Give acquisition sufficient ephemeral storage for bounded Parquet work; set results reserved concurrency to two.
- [ ] Create one ECS cluster and one Fargate task definition with `cpu = 512`, `memory = 1024`, `runtime_platform` Linux/x86_64, `readonlyRootFilesystem = true`, no port mappings, awslogs, stop timeout, task/execution roles, and the image pinned as `repository_url@sha256:digest`.
- [ ] Implement the Standard Step Functions flow with a 900-second workflow timeout:

  ```text
  AcquireData -> PrepareRun -> ecs:runTask.sync -> FinalizeSuccess
       |              |               |                  |
       v              v               +------------------+
  Closed Fail    Closed Fail                       FinalizeFailure
  ```

  Acquisition and preparation failures occur before a durable run record is guaranteed, so their catch branches must discard raw error objects and enter closed Step Functions `Fail` states. Only failures after `PrepareRun` has returned a run ID call `FinalizeFailure`. Retry only Lambda service exceptions and ECS service availability errors with bounded exponential backoff. Do not retry contract, checksum, acquisition-quality, or engine failures.
- [ ] Pass the worker only `--run-spec-key` plus the derived key from `PrepareRun`; bucket and table come from trusted task environment. Use `AssignPublicIp = ENABLED`, the fixed subnet/security group, and exactly one task.
- [ ] Create one AWS Scheduler schedule with a fixed bounded request and `state = var.enable_schedule ? "ENABLED" : "DISABLED"`. It must exist but be visibly disabled in the default plan, so reviewers can inspect the automation without risking an unattended run.
- [ ] Create one HTTP API with only `GET /runs/{run_id}`, Lambda proxy integration, access logs, stage auto-deploy, and stage-level throttle two requests/second with burst five. Do not create a POST route, list route, API key, custom domain, or wildcard CORS.
- [ ] Add CloudWatch log groups with 14-day retention before the services create defaults. Add alarms for Step Functions failures/timeouts, Lambda errors/throttles, and ECS stopped-task failures without creating an always-on polling service.
- [ ] Add one monthly AWS Budget with low actual and forecast thresholds and all validated email recipients. Document in resource descriptions that budgets alert but do not stop spend.
- [ ] Re-run Terraform format/validate/test, TFLint, Checkov, and the Python infrastructure contract tests. Resolve all failures and confirm every check passes.
- [ ] Commit only Task 10 files:

  ```text
  feat: orchestrate secured AWS batch runs
  ```

---

## Task 11: Add pull-request-safe checks and manual OIDC workflows

**Files:**

- Create: `.github/workflows/aws-checks.yml`
- Create: `.github/workflows/aws-plan.yml`
- Create: `.github/workflows/aws-deploy.yml`
- Create: `tests/cloud/test_aws_workflows.py`
- Modify: `infra/iam.tf`
- Modify: `infra/variables.tf`

**Interfaces:**

- `aws-checks.yml`: pull request/push, `contents: read`, no `id-token`, no AWS role
- `aws-plan.yml`: manual dispatch, `contents: read`, `id-token: write`, planning role only
- `aws-deploy.yml`: manual dispatch, protected `aws-demo` environment, `contents: read`, `id-token: write`, deployment role

- [ ] Write workflow tests first using `yaml.load(text, Loader=yaml.BaseLoader)`. Assert triggers, top-level permissions, manual dispatch gates, protected environment, no static access-key secret names, no `pull_request_target`, no untrusted artifact execution, lockfile use, image not pushed by checks, a saved Terraform plan before each apply, and full 40-character SHA pinning for every action.
- [ ] Run `uv run --extra dev --extra cloud pytest tests/cloud/test_aws_workflows.py -q` and confirm failure because the workflows do not exist.
- [ ] Implement `aws-checks.yml` using the repository's existing pinned checkout/setup-uv actions. Run cloud tests, full Python quality gates, dependency audit including the cloud extra, Docker build without push, Terraform format/validate/test, TFLint, and Checkov. Give it only `contents: read`.
- [ ] Implement two distinct GitHub OIDC roles in Terraform. Restrict both trust policies to the exact repository. Restrict deployment trust to the protected `aws-demo` environment subject; restrict planning trust to the approved branch/ref. Do not grant the planning role apply, ECR push, `states:StartExecution`, or broad mutation permissions.
- [ ] Implement `aws-plan.yml` as manual dispatch. Configure OIDC for the planning role, initialize the approved backend, run validation/security checks, create a saved plan, and publish only a non-sensitive plan summary. Never include provider credentials or raw state in artifacts.
- [ ] Implement `aws-deploy.yml` as manual dispatch with environment `aws-demo`. Re-run checks and use an explicitly documented first-deployment sequence: apply an exact saved foundation plan that creates ECR, build/push the immutable image, capture its registry digest, create a runtime plan using that digest, and apply that exact saved runtime plan. Later deployments skip the foundation apply. Keep workflow execution itself as a separate explicit input defaulting to false.
- [ ] Pin every action to a verified full commit SHA and retain the human-readable release version in a comment. Verify pins against each action's official repository during implementation.
- [ ] Re-run workflow tests and all YAML/security checks; confirm they pass.
- [ ] Commit only Task 11 files:

  ```text
  ci: add manual OIDC AWS delivery workflows
  ```

---

## Task 12: Document operations, cleanup, evidence, and verify the repository

**Files:**

- Create: `docs/aws-research-workflow.md`
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `Makefile`
- Modify: `tests/cloud/test_packaging.py`

**Interfaces:**

- Runbook sections: architecture, trust boundaries, local smoke, bootstrap, plan, deploy approval, smoke, publish visibility, disable, destroy, cleanup inventory, cost evidence, resume evidence
- Make targets: `cloud-test`, `cloud-infra-check`, `cloud-container-smoke`, `cloud-verify`

- [ ] Extend the packaging/documentation test first. Assert the runbook contains exact commands for bootstrap, backend init, plan, apply, disabled schedule verification, one bounded SPY smoke run, result retrieval, visibility update, log inspection, budget inspection, destroy, S3-version cleanup, ECR cleanup, log-group cleanup, and public IPv4 inventory.
- [ ] Assert `.gitignore` excludes `.terraform/`, all Terraform state/plan files, generated cloud artifacts, local result reports, `.env` variants, and credential files while retaining `.terraform.lock.hcl`.
- [ ] Run the focused test and confirm it fails because the runbook and ignore rules are incomplete.
- [ ] Write `docs/aws-research-workflow.md` with the actual resource names/outputs and copy-pasteable commands. Clearly label local validation as implemented evidence and live AWS measurements as unavailable until a separately approved deployment.
- [ ] Document the pre-apply gate in this exact order: verify $100 promotional-credit eligible products and expiration, verify account/region, verify budget recipients, review saved Terraform plan, verify schedule false, verify cleanup date/owner, obtain explicit approval.
- [ ] Document that visibility is selected on the IAM-authorized submission. A run requested as `PUBLIC` remains undiscoverable until finalization marks it `SUCCEEDED`; a `PRIVATE` terminal record is not mutated after completion. Never permit visibility changes from the public API.
- [ ] Document cleanup as an inventory-first procedure. List Terraform-managed resources, retained S3 versions/delete markers, ECR images, CloudWatch log groups, ENIs/public IPv4 addresses, the bootstrap state bucket, and AWS Budget. Keep the state bucket until all application destruction is verified.
- [ ] Add an evidence table with image digest, state-machine execution ARN, run ID, dataset/result SHA-256, duration, artifact sizes, CloudWatch query, and Cost Explorer amount. Leave values described as “record after approved smoke run,” not fabricated metrics.
- [ ] Add concise README architecture and local commands, linking the detailed runbook and design spec.
- [ ] Complete the local verification sequence:

  ```text
  uv lock --check
  uv sync --locked --extra dev --extra cloud
  make cloud-verify
  make test
  make verify-warnings
  make lint
  terraform fmt -check -recursive
  terraform -chdir=infra validate
  terraform -chdir=infra test
  tflint --chdir=infra --recursive
  checkov -d infra
  docker build --platform linux/amd64 -t algo-backtester-aws:local .
  make cloud-container-smoke
  pip-audit against the locked dev plus cloud export
  git diff --check
  ```

- [ ] Confirm coverage remains at least 80%, all pre-existing tests remain green, no network-backed test ran, no live AWS call occurred, and `git status --short` contains only the intentional branch changes plus the preserved untracked `.codegraph/`.
- [ ] Confirm the complete local verification sequence passes before making the documentation commit.
- [ ] Perform a security review of contracts, IAM, public results, OIDC trust, secret handling, presigned URLs, artifact verification, logs, and cleanup. Resolve every critical/high finding before the final commit.
- [ ] Commit only Task 12 files:

  ```text
  docs: add AWS research operations runbook
  ```

---

## Final Local Acceptance Review

- [ ] Compare every acceptance criterion in the approved spec against a test, infrastructure assertion, or documented operator gate.
- [ ] Confirm all six artifacts exist in the offline worker smoke and that the finalizer rejects any mutation.
- [ ] Confirm there is no AWS credential dependency in tests, no public compute-start path, no list endpoint, no public S3 object, no caller-selected storage location, and no mutable image reference in `RunSpec`.
- [ ] Confirm the default Terraform plan contains exactly one disabled schedule and no NAT gateway, load balancer, RDS, EFS, ECS service, VPC endpoint, customer-managed KMS key, Route 53, custom domain, WAF, or public bucket.
- [ ] Review `git diff --stat`, `git diff --check`, the complete commit sequence, and the untracked `.codegraph/` preservation.
- [ ] Stop before bootstrap/apply/push or any live smoke test and request the separate deployment approval defined in the spec.
