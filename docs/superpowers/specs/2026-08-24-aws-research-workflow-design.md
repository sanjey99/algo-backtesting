# AWS Research Workflow Design

**Status:** Written review requested
**Date:** 2026-08-24
**Repository:** `algo-backtesting`
**Branch:** `codex/aws-research-workflow`
**Region:** `ap-southeast-1` (Singapore)

## 1. Objective

Add a deployment-ready AWS research workflow around the existing algorithmic backtester without
replacing or weakening its local execution path. The cloud path must prove containerized batch
execution, workflow orchestration, immutable data and result artifacts, infrastructure as code,
least-privilege access, public read-only result inspection, and cost-aware operations.

The primary users are:

- A maintainer who starts a bounded research run through an IAM-authorized workflow.
- A recruiter or reviewer who inspects a completed public result without receiving AWS credentials.
- A maintainer diagnosing a failed acquisition or backtest through structured CloudWatch events.

The intended portfolio outcome is a reproducible research data product, not merely a hosted
dashboard:

```text
validated acquisition -> pinned dataset -> bounded batch backtest
  -> immutable result artifacts -> public read-only result
```

## 2. Existing Boundaries Preserved

The implemented repository remains authoritative:

- `AcquisitionService` validates, normalizes, quality-gates, and records lineage for market data.
- `DataStore` publishes immutable local Parquet generations using filesystem locks, atomic replace,
  and fsync semantics.
- `BacktestEngine.run()` executes the event-driven backtest and returns `BacktestResult`.
- `compute_all_metrics()` and the report module produce analytics and standalone HTML.
- FastAPI persists local runs through SQLAlchemy and SQLite.
- FastAPI permutation jobs use a deliberately process-local `InMemoryJobStore`.
- JSON operational logging already rejects invalid fields and redacts secrets and credential-bearing
  URLs.

The cloud subsystem is additive. It must not:

- Replace `DataStore` with an S3 implementation that pretends object storage has filesystem rename
  or lock semantics.
- Treat SQLite as shared Fargate persistence.
- reuse the in-memory FastAPI job store for distributed workflow state.
- Change the existing local API, dashboard, SQL analytics, or database behavior.
- Upload raw provider responses or publish canonical market-data bars to public consumers.

## 3. Approaches Considered

### 3.1 Deploy the existing FastAPI and SQLite application to ECS

Rejected. SQLite is process-local, the async job store is in memory, and the current application
was not designed as a horizontally scalable service. Hosting it unchanged would add an AWS label
without a correct distributed state model.

### 3.2 Rebuild all persistence around S3 and DynamoDB

Rejected for Phase 1. Replacing mature local cache and SQL behavior would create a migration rather
than a bounded cloud extension. It would also blur which guarantees are verified locally and which
exist in AWS.

### 3.3 Add a separate cloud research workflow — selected

Use the existing acquisition, engine, analytics, report, and observability boundaries behind new
cloud adapters. S3 owns immutable cloud artifacts; DynamoDB owns workflow metadata; Step Functions
owns orchestration; one-shot Fargate tasks own paid computation. Existing local behavior remains
unchanged and is covered by its current test suite.

## 4. Architecture

```text
IAM-authorized CLI or protected GitHub workflow
                     |
                     v
               Step Functions
                     |
       AcquireData Lambda container
                     |
       private S3 dataset + manifest
                     |
        PrepareRun Lambda container
          |                     |
          v                     v
  private S3 run spec      DynamoDB PENDING
          |
          v
   ECS Fargate one-shot worker
          |
          v
 private S3 result/report/checksums
          |
          v
       FinalizeRun Lambda
          |
          v
   DynamoDB SUCCEEDED or FAILED

Public GET /runs/{run_id}
          |
          v
 API Gateway HTTP API -> Results Lambda
          |                 |
          |                 +-> bounded DynamoDB summary
          |                 +-> short-lived report URL for PUBLIC runs
          v
 no endpoint capable of starting paid compute
```

Cross-cutting controls are Terraform, GitHub Actions OIDC, least-privilege IAM, structured
CloudWatch logs, explicit timeouts, retention policies, and AWS Budget alerts.

## 5. Public and Private Access Model

### 5.1 Private write path

Only an IAM-authorized principal may call `states:StartExecution` for the workflow. Phase 1 supports
two callers:

- A maintainer using the AWS CLI with their own authenticated AWS session.
- A manually dispatched GitHub Actions deployment environment using OIDC.

There is no public `POST`, public task launcher, API key, long-lived AWS access key, or browser
submission form. Pull-request workflows cannot assume an apply-capable or execution-capable role.

### 5.2 Public read path

The HTTP API exposes only `GET /runs/{run_id}`. A record is returned only when all of the following
hold:

- The path parameter is a canonical UUID.
- The DynamoDB record exists.
- Its status is `SUCCEEDED`.
- Its visibility is `PUBLIC`.
- The referenced result manifest has passed finalization.

All other cases return the same bounded `404` response. The caller cannot provide an S3 bucket,
key, prefix, query, strategy, or output size. The response contains summary metadata and metrics,
not raw bars, trade-by-trade payloads, or equity-curve arrays. A report link is a five-minute S3
presigned URL derived by the Lambda from the finalized record.

API Gateway route throttling and Lambda reserved concurrency bound accidental or abusive reads.
Phase 1 has no public list endpoint, custom domain, WAF, or user authentication system.

## 6. Cloud Contracts

### 6.1 Dataset reference

`DatasetRef` contains:

- Schema version `1`.
- Validated S3 bucket and key under `datasets/v1/`.
- SHA-256 of the exact Parquet object.
- Acquisition manifest key and SHA-256.
- Normalized symbol, calendar, interval, inclusive start date, and inclusive end date.
- Acquisition identifier and completion time.

Workers never resolve a `latest` pointer. Every run pins an exact object key and digest.

### 6.2 Run specification

`RunSpec` contains:

- Schema version `1` and a UUID run identifier.
- The complete pinned `DatasetRef`.
- A strategy key from the existing strategy registry and validated parameters.
- Finite positive initial capital and finite nonnegative commission and slippage percentages.
- Engine image digest in `sha256:<64 lowercase hexadecimal characters>` form.
- Visibility `PRIVATE` or `PUBLIC`.
- Creation time, maximum runtime, and derived output prefix.

Unknown fields are rejected. Object keys must use a closed grammar, start with their required
prefix, and reject empty path components, dot components, backslashes, control characters, and
overlong values. Run IDs, output prefixes, and result keys are derived from admitted values rather
than accepted directly from callers.

### 6.3 Run record

DynamoDB uses one item per run:

```text
PK                 RUN#{uuid}
status             PENDING | RUNNING | SUCCEEDED | FAILED
visibility         PRIVATE | PUBLIC
dataset_key        datasets/v1/...
dataset_sha256     64 lowercase hexadecimal characters
run_spec_key       runs/v1/{run_id}/run-spec.json
result_prefix      runs/v1/{run_id}/
image_digest       sha256:...
created_at         UTC RFC 3339
started_at         UTC RFC 3339 or absent
completed_at       UTC RFC 3339 or absent
failure_code       closed public-safe code or absent
expires_at         DynamoDB TTL epoch seconds
```

State transitions use DynamoDB conditional expressions:

```text
absent -> PENDING -> RUNNING -> SUCCEEDED
                            -> FAILED
              PENDING      -> FAILED
```

Terminal records are immutable except for an idempotent replay of the same terminal outcome.
Detailed exceptions go only to redacted logs; public records contain closed failure codes.

### 6.4 Result artifacts

The worker publishes beneath a derived prefix:

```text
runs/v1/{run_id}/run-spec.json
runs/v1/{run_id}/result.json
runs/v1/{run_id}/trades.parquet
runs/v1/{run_id}/equity-curve.parquet
runs/v1/{run_id}/report.html
runs/v1/{run_id}/checksums.json
```

`checksums.json` lists the schema version, exact required object names, content lengths, and SHA-256
digests. The finalizer independently fetches and verifies every required artifact before marking
the run `SUCCEEDED`. A partial upload never becomes public.

## 7. Execution Flow

1. Step Functions validates the bounded workflow input shape and invokes acquisition.
2. The acquisition handler admits one daily US-equity request, creates isolated `/tmp` cache and
   manifest directories, and calls the existing `AcquisitionService`.
3. Only a successful or admitted partial-success canonical frame and its redacted manifest are
   uploaded. Typed acquisition failures enter the workflow failure path.
4. The preparation handler creates a UUID run ID, immutable run specification, and conditional
   `PENDING` record.
5. Step Functions calls ECS `RunTask` with `.sync`, one task, a fixed task definition, and only the
   run-spec bucket/key as command input.
6. The worker admits the run spec, conditionally marks the record `RUNNING`, downloads the exact
   Parquet object, verifies SHA-256, converts validated bars to candles, resolves the registered
   strategy, and calls `BacktestEngine.run()`.
7. The worker computes metrics, generates the standalone report, publishes artifacts, and exits
   zero. It never decides that the run is public or terminally successful.
8. The success finalizer verifies all required artifacts and conditionally writes `SUCCEEDED`.
9. A `Catch` path sends a closed failure code to the failure finalizer, which conditionally writes
   `FAILED`. Raw AWS error objects are not copied into DynamoDB or public responses.
10. The public results handler reads only finalized public metadata and creates a short-lived report
    URL from a server-derived key.

Workflow retries apply only to explicitly retryable service failures. Acquisition validation,
checksum mismatch, contract failure, and engine failure fail without retrying paid compute.

## 8. Runtime Packaging

One Linux/x86 Python 3.12 image is built from the locked repository dependencies. It contains the
AWS Lambda Runtime Interface Client plus the project and supports two runtime modes:

- Lambda sets the image command to a specific handler.
- ECS overrides the command to invoke `python -m src.cloud.worker`.

The image runs as a non-root user where the runtime permits it, writes only beneath `/tmp`, exposes
no port, contains no credentials, and has a Docker health/build smoke check. The image digest used
by the worker task is pinned into each `RunSpec`; mutable tags are not execution evidence.

## 9. AWS Resources

Terraform creates:

- One private, versioned S3 artifact bucket with bucket-owner enforcement, SSE-S3, public access
  block, lifecycle rules, and no ACLs.
- One DynamoDB table in on-demand mode with point-in-time recovery disabled for the short-lived demo
  and TTL enabled.
- One private ECR repository with scan-on-push and a lifecycle retaining at most three tagged images.
- One VPC, one small public subnet, an Internet Gateway, route table, and a task security group with
  no inbound rules and HTTPS egress.
- One ECS cluster and one task definition. There is no ECS service or desired count.
- Lambda functions for acquisition, preparation, finalization, and public result reads.
- One Step Functions Standard state machine and required EventBridge/ECS integration roles.
- One HTTP API with only the public GET route.
- CloudWatch log groups with fourteen-day retention and bounded alarms.
- One EventBridge schedule resource disabled by default.
- AWS Budget actual and forecast alerts.
- GitHub OIDC provider/roles restricted to this repository, branch or protected environment, and
  exact plan/deploy purposes.

Terraform deliberately does not create a NAT Gateway, load balancer, RDS, EFS, VPC endpoint,
customer-managed KMS key, ECS service, GPU resource, Route 53 zone, custom domain, WAF, or public S3
bucket.

## 10. Terraform State and Deployment

`infra/bootstrap/` contains a separately applied bootstrap stack for a private, versioned,
SSE-S3 Terraform state bucket with public access blocked and native S3 lockfile support. Bootstrap
and application deployment are separate approval events. No backend credential is stored in the
repository.

GitHub Actions contains:

- `aws-checks.yml`: pull-request-safe formatting, validation, security scanning, Python cloud tests,
  and a Docker build. It has `contents: read` and no AWS identity permission.
- `aws-plan.yml`: manually dispatched OIDC-authenticated plan using a read-only planning role.
- `aws-deploy.yml`: manually dispatched deployment using a protected `aws-demo` environment,
  `contents: read`, and `id-token: write`. It never runs on push or pull request.

Actions are pinned to full commit SHAs. The deploy role trust policy admits only this repository and
the protected environment/ref. No static AWS access key is added to GitHub secrets.

## 11. IAM Boundaries

- Acquisition Lambda: write only the derived `datasets/v1/` keys and its own logs.
- Preparation Lambda: read admitted dataset metadata; write a derived run spec and create one
  `PENDING` item.
- Fargate execution role: pull the pinned private ECR image and write logs.
- Fargate task role: read the exact dataset/run-spec prefixes, write only the matching result
  prefix, and conditionally update only the matching run record.
- Finalizer Lambda: read required result objects and conditionally update run terminal state; no
  object writes.
- Results Lambda: read DynamoDB run summaries and finalized report objects; no writes and no dataset
  access.
- GitHub plan role: read infrastructure and generate a plan; no apply or workflow-execution access.
- GitHub deploy role: manage only resources carrying the project/environment boundary required by
  the application stack.

Alpha Vantage is excluded from Phase 1. If later enabled, its key must enter through Secrets Manager
and runtime IAM, never Terraform variables/state, task definitions, run specs, GitHub variables, or
logs.

## 12. Cost Controls

The safe defaults are:

- `enable_schedule = false`.
- No ECS service; Step Functions starts exactly one task.
- Fargate starts at 0.5 vCPU and 1 GB memory, with measurement-driven downsizing permitted later.
- Worker runtime limit ten minutes; whole workflow limit fifteen minutes.
- API route throttle two requests/second with burst five.
- Results Lambda reserved concurrency two.
- CloudWatch retention fourteen days.
- ECR retains at most three tagged images.
- DynamoDB TTL forty-five days.
- Transient datasets and private runs expire after forty-five days; selected public reports may be
  retained ninety days.
- Every supported resource is tagged with project, environment, owner, expiry date, and cost center.
- Actual and forecast budget notifications are configured at low thresholds, but documentation
  states that alerts are not a reliable kill switch.

For the review workload of one hundred ten-minute runs at 0.5 vCPU and 1 GB, the expected Singapore
Fargate compute is approximately USD 0.51, with approximately USD 0.08 of public IPv4 time. The
complete forty-day demo is expected to consume less than USD 1.50 under the stated traffic and
artifact assumptions, excluding taxes and domain registration. The operator must verify the
promotional credit's applicable-products list before the first apply.

## 13. Testing

Python tests are offline and dependency-injected. They never require AWS credentials or live
network calls.

- Contract tests reject unknown fields, unsupported strategies, non-finite numbers, unsafe keys,
  traversal-shaped values, invalid hashes, invalid UUIDs, and caller-supplied output prefixes.
- Acquisition-handler tests prove that only admitted acquisition results are serialized and that
  manifest redaction survives object publication.
- Preparation tests prove deterministic derived keys, conditional run creation, and immutable
  dataset/image pinning.
- Worker tests run fixture Parquet through the real engine and report code, verify deterministic
  schema-valid outputs, and fail before execution on checksum mismatch.
- Finalizer tests reject missing, extra, oversized, and tampered artifacts and prove idempotent
  terminal transitions.
- Results-handler tests return only `PUBLIC` plus `SUCCEEDED` summaries, derive report keys
  internally, cap response size, and produce indistinguishable not-found responses.
- Storage-adapter tests assert exact boto3 request shapes through injected clients; higher-level
  tests use in-memory fakes rather than mocks of business behavior.
- Existing API, engine, acquisition, SQL, dashboard, and database tests remain unchanged and green.

Infrastructure checks are:

```text
terraform fmt -check -recursive
terraform validate
tflint --recursive
checkov -d infra
docker build
docker run local fixture smoke
```

A live smoke test is a separate approved action: acquire one bounded SPY range, run one Fargate
task, retrieve one public result, inspect CloudWatch logs, record actual cost, disable unused
resources, and execute the documented cleanup path.

## 14. Repository Layout

```text
src/cloud/
  __init__.py
  contracts.py
  storage.py
  ingestion_handler.py
  prepare_handler.py
  worker.py
  finalize_handler.py
  results_handler.py

tests/cloud/
  __init__.py
  fakes.py
  test_contracts.py
  test_storage.py
  test_ingestion_handler.py
  test_prepare_handler.py
  test_worker.py
  test_finalize_handler.py
  test_results_handler.py

infra/
  bootstrap/
  versions.tf
  providers.tf
  variables.tf
  locals.tf
  storage.tf
  dynamodb.tf
  ecr.tf
  network.tf
  iam.tf
  lambda.tf
  ecs.tf
  step_functions.tf
  eventbridge.tf
  api_gateway.tf
  cloudwatch.tf
  budgets.tf
  outputs.tf

.github/workflows/
  aws-checks.yml
  aws-plan.yml
  aws-deploy.yml

Dockerfile
.dockerignore
docs/aws-research-workflow.md
```

Files remain split by responsibility. The cloud adapters consume existing public engine and data
contracts; they do not move cloud conditionals into the local engine, acquisition service, or API.

## 15. Documentation and Evidence

The operational guide records:

- Architecture and trust boundaries.
- Local fixture execution.
- Bootstrap, plan, apply, smoke, disable, and destroy commands.
- Credit applicability and budget checks required before apply.
- Exact resources intentionally omitted for cost reasons.
- How to mark a finalized run public.
- A cleanup inventory that detects retained ECR images, log groups, S3 versions, and public IPv4
  addresses.
- The distinction between measured live evidence and design intent.

No resume metric is written until a live run records the exact image digest, workflow execution,
duration, artifact checksums, logs, and AWS cost evidence.

## 16. Acceptance Criteria

Phase 1 local implementation is complete when:

1. All new Python behavior followed red-green-refactor and the full existing suite remains green.
2. Ruff and strict mypy pass.
3. The locked dependency set is reproducible and audited.
4. A fixture dataset completes the local worker path and produces all required verified artifacts.
5. Public result reads cannot start computation, enumerate runs, choose storage keys, or expose raw
   datasets.
6. Terraform formatting, validation, lint, and security checks pass with scheduling disabled.
7. The container builds and completes the offline fixture smoke test as a non-root process.
8. CI pull-request checks have no AWS write identity.
9. Plan and deploy workflows use restricted OIDC roles and manual dispatch only.
10. Cleanup and cost-runbook documentation exists before any live apply.

Live AWS deployment is not part of local implementation completion. It requires a separate user
approval after reviewing `terraform plan`, the promotional credit's applicable products, the
configured budget recipients, and the cleanup procedure.
