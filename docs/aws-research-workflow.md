# AWS research workflow operations

This runbook is the human-operated boundary for the cost-bounded AWS research
workflow. It documents a deployment procedure; it does not authorize one.
All local checks below are credential-free. No live AWS values have been
measured for this repository: account IDs, ARNs, bucket names, image digests,
execution IDs, query results, durations, bytes, and costs remain **record after
approved smoke run**.

Unless a snippet explicitly says otherwise, operator snippets assume one Bash
session and fail-fast semantics; do not copy isolated lines into a different
shell without carrying forward their validated variables and evidence paths.

## Architecture and trust boundaries

The local application remains unchanged. The AWS path is a separate, one-shot
workflow: a private artifact bucket holds immutable datasets and result
receipts; DynamoDB holds bounded run state; Step Functions invokes acquisition,
preparation, one Fargate worker, and finalization; API Gateway exposes only
`GET /runs/{run_id}` through the results Lambda. It has no list or compute-start
endpoint. A disabled EventBridge Scheduler is an opt-in future trigger, not an
active schedule.

Terraform names resources from `project-environment` (for example,
`algo-demo-research`), but generated S3 bucket names must always be read from
outputs. Runtime outputs are `state_machine_arn`, `public_results_api_base_url`,
`ecs_cluster_name`, `ecs_worker_task_definition_arn`, `runtime_log_group_names`,
`budget_name`, `schedule_enabled`, `run_table_name`, `vpc_id`,
`task_subnet_id`, `task_security_group_id`, `ecr_repository_name`,
`ecr_repository_url`, and `artifact_bucket_name`. Bootstrap outputs are
`state_bucket_name` and `backend_region`.

GitHub Actions obtains short-lived credentials through two OIDC roles. The plan
role is bound to one repository and exact ref; the deployment role is bound to
the `aws-demo` environment subject. Runtime service roles are capped by the
separate permissions boundary. No static AWS key is required or accepted by the
workflow.

## Local smoke and local-only verification evidence

Run these before any human considers an AWS action. They do not call AWS,
Terraform apply/destroy, ECR push, or GitHub workflow dispatch. The runtime
test container executes with `--network none`, a read-only filesystem, a
non-root user, and only a bounded `/tmp` tmpfs. Its image build can require
locally cached or registry-provided build material; only the runtime container
is network-isolated.

```bash
uv lock --check
uv sync --locked --extra dev --extra cloud
make cloud-verify
make test
make verify-warnings
make lint
```

`make cloud-test` executes the offline `tests/cloud` suite. `make cloud-smoke`
executes a local fake-store worker receipt. `make cloud-container-smoke` tests
the real image with `--network none`; it does not use AWS credentials.
`make cloud-infra-check` runs Terraform format/validate/native tests, TFLint,
and Checkov only; it never runs `terraform apply`.

If the local Terraform provider or scanner cache is unavailable, stop and
restore the approved local tooling instead of replacing these checks with a
live plan. Local validation is implementation evidence, not evidence that AWS
resources exist.

## Hard GitHub environment prerequisite — before bootstrap

Do **not** create the OIDC provider, roles, or state bucket until a repository
administrator has configured the `aws-demo` GitHub environment. The selected
deployment ref only, at least one required reviewer, prevention of self-review,
and disabled administrator bypass are all mandatory. Environment-protected jobs
wait before they receive environment access, so the OIDC deployment subject is
not trustworthy without this configuration.

The following is read-only REST evidence. It needs only a token that can read
Actions metadata (`actions:read`); it changes no GitHub or AWS state. It reads `GET /repos/{owner}/{repo}/environments/aws-demo` and its deployment-branch-policy endpoint. Set the one approved ref and pattern once, then use the same ref as `TF_VAR_deploy_ref` during bootstrap.

```bash
export GH_REPO='OWNER/REPOSITORY'
export APPROVED_DEPLOYMENT_REF='refs/heads/main'
export APPROVED_DEPLOYMENT_PATTERN="${APPROVED_DEPLOYMENT_REF#refs/heads/}"
export GH_API_VERSION='2026-03-10'
set -euo pipefail
umask 077
export EVIDENCE_DIRECTORY="$(mktemp -d "${TMPDIR:-/tmp}/algo-aws-evidence.XXXXXX")"
chmod 700 "$EVIDENCE_DIRECTORY"

gh api --method GET \
  -H 'Accept: application/vnd.github+json' \
  -H "X-GitHub-Api-Version: ${GH_API_VERSION}" \
  "/repos/${GH_REPO}/environments/aws-demo" > "$EVIDENCE_DIRECTORY/environment.json"
gh api --method GET \
  -H 'Accept: application/vnd.github+json' \
  -H "X-GitHub-Api-Version: ${GH_API_VERSION}" \
  "/repos/${GH_REPO}/environments/aws-demo/deployment-branch-policies" \
  > "$EVIDENCE_DIRECTORY/branch-policies.json"

jq -e '
  ([.protection_rules[] | select(.type == "required_reviewers") | .reviewers[]] | length > 0)
  and ([.protection_rules[] | select(.type == "required_reviewers") | .prevent_self_review] | all)
  and (.can_admins_bypass == false)
  and (.deployment_branch_policy.custom_branch_policies == true)
  and (.deployment_branch_policy.protected_branches == false)
' "$EVIDENCE_DIRECTORY/environment.json"
jq -e --arg pattern "$APPROVED_DEPLOYMENT_PATTERN" '
  (.branch_policies | length == 1)
  and (.branch_policies[0].name == $pattern)
' "$EVIDENCE_DIRECTORY/branch-policies.json"
```

Record both JSON responses with the deployment change record (not in this
repository) and stop if either assertion fails. The planned subject is exactly
`repo:OWNER/REPOSITORY:environment:aws-demo`; a branch policy must therefore
restrict the ref independently. Do not accept an administrator bypass, a blank
reviewer list, self-review, protected-branches-only mode, or an additional
branch/tag pattern. The read response does not reliably expose the policy kind;
record separate administrator evidence that the single approved pattern was
created as a **branch** policy, not a tag policy, before trusting it.
The restrictive `EVIDENCE_DIRECTORY` holds all transient command receipts for
this procedure; retain the required evidence in the approved secure system
outside the repository, then remove the local directory. Never commit it.

## Human pre-apply gate

Complete and record these seven checks in this exact order before either a
bootstrap or runtime apply. The final approval is explicit, written, and tied
to the saved plan checksum.

1. Verify the USD 100 promotional-credit eligible products and expiration.
2. Verify AWS account and region.
3. Verify budget recipients.
4. Review the saved Terraform plan.
5. Verify the schedule is false/disabled.
6. Verify cleanup date and owner.
7. Obtain explicit approval.

For the first three checks, use the AWS Console or documented account support
channels appropriate to the account. Do not assume promotional credits cover
any service. Capture the account and region through read-only calls, validate
them, and set non-secret inputs deliberately. For later deployments, inspect
actual budget subscribers before a new plan. For a first deployment, verify the
intended recipients from the newly created saved runtime plan before approving
its runtime apply:

```bash
export AWS_REGION='ap-southeast-1'
export TF_VAR_region="$AWS_REGION"
export TF_VAR_project='YOUR_PROJECT'
export TF_VAR_environment='demo'
export TF_VAR_owner='APPROVED_OWNER'
export TF_VAR_cost_center='APPROVED_COST_CENTER'
export TF_VAR_expiry_date='YYYY-MM-DD'
export TF_VAR_alert_emails='["owner@example.com"]'
export TF_VAR_github_repository="$GH_REPO"
export TF_VAR_deploy_ref="$APPROVED_DEPLOYMENT_REF"
export TF_VAR_deploy_environment='aws-demo'
export TF_VAR_backend_state_key='terraform.tfstate'
export TF_VAR_enable_schedule=false
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]
[[ "$AWS_REGION" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]]
REGION_FROM_AWS="$(aws ec2 describe-availability-zones --region "$AWS_REGION" \
  --all-availability-zones --query 'AvailabilityZones[0].RegionName' --output text)"
test "$REGION_FROM_AWS" = "$AWS_REGION"
BUDGET_NAME="${TF_VAR_project}-${TF_VAR_environment}-monthly-guardrail"
export FIRST_DEPLOYMENT=false # true only while the budget is absent before its first apply
case "$FIRST_DEPLOYMENT" in
  true)
    printf '%s\n' 'First deployment: do not query the absent budget or use the later-only local manual plan.'
    printf '%s\n' 'Before approving the AWS deploy runtime-apply environment job, inspect its human-readable runtime-plan summary and associated exact checksummed plan artifact.'
    printf '%s\n' 'Verify its exact alert email set and schedule_enabled=false; this is the first-deployment budget/schedule evidence.'
    ;;
  false)
    if ! aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" \
      > "$EVIDENCE_DIRECTORY/budget.json" 2> "$EVIDENCE_DIRECTORY/budget-error.txt"; then
      cat "$EVIDENCE_DIRECTORY/budget-error.txt" >&2; exit 1
    fi
    if ! BUDGET_NOTIFICATIONS="$(aws budgets describe-notifications-for-budget \
      --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" --query Notifications --output json \
      2> "$EVIDENCE_DIRECTORY/budget-notifications-error.txt")"; then
      cat "$EVIDENCE_DIRECTORY/budget-notifications-error.txt" >&2; exit 1
    fi
    jq -e 'length > 0' <<< "$BUDGET_NOTIFICATIONS"
    BUDGET_NOTIFICATION_INDEX=0
    while IFS= read -r BUDGET_NOTIFICATION; do
      BUDGET_SUBSCRIBERS_RECEIPT="$EVIDENCE_DIRECTORY/budget-subscribers-${BUDGET_NOTIFICATION_INDEX}.json"
      aws budgets describe-subscribers-for-notification --account-id "$ACCOUNT_ID" \
        --budget-name "$BUDGET_NAME" --notification "$BUDGET_NOTIFICATION" \
        > "$BUDGET_SUBSCRIBERS_RECEIPT"
      jq -e --argjson expected "$TF_VAR_alert_emails" '
        ([.Subscribers[] | select(.SubscriptionType == "EMAIL") | .Address] | sort)
        == ($expected | sort)
      ' "$BUDGET_SUBSCRIBERS_RECEIPT"
      BUDGET_NOTIFICATION_INDEX=$((BUDGET_NOTIFICATION_INDEX + 1))
    done < <(jq -c '.[]' <<< "$BUDGET_NOTIFICATIONS")
    ;;
  *) printf '%s\n' 'FIRST_DEPLOYMENT must be true or false' >&2; exit 2 ;;
esac
```

For the initial budget creation, confirm the planned `TF_VAR_alert_emails` in
the completed `AWS deploy` `runtime-plan` job; before approving the subsequent
`runtime-apply` environment job, verify the human-readable summary and the
associated exact checksummed plan artifact have the expected recipients and
`schedule_enabled=false`. After the first approved apply, set
`FIRST_DEPLOYMENT=false` and run the actual subscriber loop above before any
smoke execution. The later-deployment local manual plan is not used for
first-deployment recipient or schedule evidence. No describe error may be treated as first deployment; every later-deployment describe error fails closed.

## Bootstrap state backend and one-time OIDC setup

Bootstrap uses a separately approved human AWS session. GitHub workflows cannot
assume their OIDC roles until the OIDC provider and roles exist, which is why
this one-time manual bootstrap precedes the workflow. First create the private,
versioned state bucket with a saved, targeted plan; do not use a generated name
as an input.

```bash
terraform -chdir=infra/bootstrap init -input=false -backend=false
terraform -chdir=infra/bootstrap plan -input=false -out=bootstrap-state.tfplan \
  -target=aws_s3_bucket.state \
  -target=aws_s3_bucket_ownership_controls.state \
  -target=aws_s3_bucket_public_access_block.state \
  -target=aws_s3_bucket_versioning.state \
  -target=aws_s3_bucket_server_side_encryption_configuration.state \
  -target=aws_s3_bucket_policy.state_tls_only \
  -target=aws_s3_bucket_lifecycle_configuration.state
sha256sum infra/bootstrap/bootstrap-state.tfplan
terraform -chdir=infra/bootstrap show -no-color bootstrap-state.tfplan
# Perform step 7 of the pre-apply gate before this destructive-state-changing command:
terraform -chdir=infra/bootstrap apply -input=false bootstrap-state.tfplan
export TF_STATE_BUCKET="$(terraform -chdir=infra/bootstrap output -raw state_bucket_name)"
export TF_STATE_REGION="$(terraform -chdir=infra/bootstrap output -raw backend_region)"
```

After the hard environment prerequisite and the same pre-apply gate, create
only the OIDC provider, the plan/deploy roles and their inline policies, and the
runtime permission boundary from a second saved targeted plan:

```bash
terraform -chdir=infra/bootstrap plan -input=false -out=bootstrap-oidc.tfplan \
  -target=aws_iam_openid_connect_provider.github_actions \
  -target=aws_iam_policy.runtime_permissions_boundary \
  -target=aws_iam_role.github_plan \
  -target=aws_iam_role.github_deploy \
  -target=aws_iam_role_policy.github_plan \
  -target=aws_iam_role_policy.github_deploy
sha256sum infra/bootstrap/bootstrap-oidc.tfplan
terraform -chdir=infra/bootstrap show -no-color bootstrap-oidc.tfplan
# Perform step 7 of the pre-apply gate before this destructive-state-changing command:
terraform -chdir=infra/bootstrap apply -input=false bootstrap-oidc.tfplan
```

Set GitHub repository variables only after that human-reviewed apply. Read the
role ARNs only in the approved live procedure, because they are intentionally
not Terraform outputs:

```bash
aws iam get-role --role-name "${TF_VAR_project}-${TF_VAR_environment}-github-plan" \
  --query 'Role.Arn' --output text
aws iam get-role --role-name "${TF_VAR_project}-${TF_VAR_environment}-github-deploy" \
  --query 'Role.Arn' --output text
```

## Backend initialization, plans, and protected deployment

Every application command initializes the generated state bucket explicitly
with S3 locking enabled. Never commit the resulting state, lock, plan, or
operator variable files. Capture the immutable worker image from the approved
ECR image before a **later-deployment-only** manual runtime plan; the repository
and image do not exist before the first foundation deployment. Terraform accepts
the raw 64-hex digest, not an image tag or a fabricated placeholder.

```bash
export ECR_REPOSITORY_NAME="${TF_VAR_project}-${TF_VAR_environment}-worker"
export APPROVED_IMAGE_TAG='GIT_SHA_OF_APPROVED_IMAGE'
[[ "$ECR_REPOSITORY_NAME" =~ ^[a-z0-9][a-z0-9-]{1,254}$ ]]
[[ "$APPROVED_IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]
IMAGE_DIGEST="$(aws ecr describe-images --repository-name "$ECR_REPOSITORY_NAME" \
  --image-ids imageTag="$APPROVED_IMAGE_TAG" \
  --query 'imageDetails[0].imageDigest' --output text)"
IMAGE_DIGEST="${IMAGE_DIGEST#sha256:}"
[[ "$IMAGE_DIGEST" =~ ^[0-9a-f]{64}$ ]]

terraform -chdir=infra init -input=false \
  -backend-config="bucket=${TF_STATE_BUCKET}" \
  -backend-config="key=${TF_VAR_backend_state_key}" \
  -backend-config="region=${TF_STATE_REGION}" \
  -backend-config="use_lockfile=true"
terraform -chdir=infra plan -input=false -out=manual.tfplan \
  -var="image_digest=${IMAGE_DIGEST}"
terraform -chdir=infra show -json manual.tfplan > "$EVIDENCE_DIRECTORY/manual-plan.json"
jq -e '.planned_values.outputs.schedule_enabled.value == false' \
  "$EVIDENCE_DIRECTORY/manual-plan.json"
terraform -chdir=infra show -no-color manual.tfplan
```

Do not read `terraform output` as evidence for this unapplied manual plan. A
live `terraform output -raw schedule_enabled` equality check is a post-apply
verification only; the disable flow below performs it immediately after apply.

`AWS plan` is a preview-only summary from the exact approved ref and plan role;
it is not the artifact applied by deployment. `AWS deploy` generates its own
separately checksummed foundation/runtime saved plans and presents each exact
artifact to `aws-demo`; required reviewers approve before the deploy role applies
it. Do not apply a locally regenerated plan in place of the reviewed deployment
plan.

### First deployment versus later deployment

For a first deployment, dispatch `AWS deploy` with `first_deployment=true` and
`execute_workflow=false`. It creates the ECR foundation from a reviewed plan,
then builds/pushes the immutable image and applies the reviewed runtime plan.
For later deployments, use `first_deployment=false`; the existing ECR
repository receives a new image digest before the saved runtime plan is
reviewed. Leave `execute_workflow=false` unless the explicit one-run smoke has
also passed the seven-step gate.

### Mandatory post-deploy image-digest receipt

After every successful `AWS deploy`—including the first deployment—and before
any smoke or cleanup path, run this read-only receipt capture. It obtains the
deployed task definition and repository URL from Terraform outputs, then proves
the single `worker` container uses that exact repository plus a lower-case
64-hex SHA-256 digest. It exports `IMAGE_DIGEST` for later disable/destroy
saved plans; the earlier manual ECR lookup remains later-deployment-only.

```bash
set -euo pipefail
WORKER_TASK_DEFINITION_ARN="$(terraform -chdir=infra output -raw ecs_worker_task_definition_arn)"
ECR_REPOSITORY_URL="$(terraform -chdir=infra output -raw ecr_repository_url)"
[[ "$WORKER_TASK_DEFINITION_ARN" =~ ^arn:aws(-[a-z]+)?:ecs:${AWS_REGION}:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[0-9]+$ ]]
[[ "$ECR_REPOSITORY_URL" =~ ^[0-9]{12}\.dkr\.ecr\.${AWS_REGION}\.amazonaws\.com/[a-z0-9][a-z0-9-]{1,254}$ ]]
aws ecs describe-task-definition --task-definition "$WORKER_TASK_DEFINITION_ARN" \
  --output json > "$EVIDENCE_DIRECTORY/worker-task-definition.json"
WORKER_IMAGE="$(jq -er '
  [.taskDefinition.containerDefinitions[] | select(.name == "worker") | .image]
  | if length == 1 and .[0] != null then .[0] else error("expected exactly one worker container image") end
' "$EVIDENCE_DIRECTORY/worker-task-definition.json")"
IMAGE_DIGEST="${WORKER_IMAGE#"$ECR_REPOSITORY_URL@sha256:"}"
EXPECTED_WORKER_IMAGE="${ECR_REPOSITORY_URL}@sha256:${IMAGE_DIGEST}"
test "$WORKER_IMAGE" = "$EXPECTED_WORKER_IMAGE"
[[ "$IMAGE_DIGEST" =~ ^[0-9a-f]{64}$ ]]
export IMAGE_DIGEST
jq -n --arg task_definition_arn "$WORKER_TASK_DEFINITION_ARN" \
  --arg ecr_repository_url "$ECR_REPOSITORY_URL" --arg image "$WORKER_IMAGE" \
  --arg image_digest "$IMAGE_DIGEST" \
  '{task_definition_arn: $task_definition_arn, ecr_repository_url: $ecr_repository_url, image: $image, image_digest: $image_digest}' \
  > "$EVIDENCE_DIRECTORY/worker-image-receipt.json"
jq -e '.image == (.ecr_repository_url + "@sha256:" + .image_digest)
  and (.image_digest | test("^[0-9a-f]{64}$"))' \
  "$EVIDENCE_DIRECTORY/worker-image-receipt.json"
```

## One bounded SPY smoke execution

The optional Task 11 smoke request is fixed, bounded, and private. Start it at
most once after an approved runtime apply; it is a paid AWS action and is not a
local verification command. Manual `start-execution` and the `AWS deploy`
`execute_workflow=true` input are mutually exclusive: choose exactly one to
prevent duplicate paid runs. This runbook uses the manual path below.

```bash
export STATE_MACHINE_ARN="$(terraform -chdir=infra output -raw state_machine_arn)"
EXECUTION_NAME="manual-$(date -u +%Y%m%d%H%M%S)-1"
[[ "$EXECUTION_NAME" =~ ^[A-Za-z0-9_-]{1,80}$ ]]
EXECUTION_ARN="$(aws stepfunctions start-execution \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --name "$EXECUTION_NAME" \
  --input '{"schema_version":"1","symbol":"SPY","start":"2024-01-02","end":"2024-01-10","strategy_key":"ma_crossover","strategy_parameters":{"fast_window":10,"slow_window":20},"initial_capital":10000.0,"commission_pct":0.001,"slippage_pct":0.0005,"visibility":"PRIVATE"}' \
  --query executionArn --output text)"
[[ "$EXECUTION_ARN" =~ ^arn:aws[a-z-]*:states:${AWS_REGION}:[0-9]{12}:execution:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$ ]]
SMOKE_STARTED_AT="$(date +%s)"
for _ in $(seq 1 91); do
  aws stepfunctions describe-execution --execution-arn "$EXECUTION_ARN" > "$EVIDENCE_DIRECTORY/smoke-execution.json"
  STATUS="$(jq -r .status "$EVIDENCE_DIRECTORY/smoke-execution.json")"
  case "$STATUS" in
    RUNNING) sleep 10 ;;
    SUCCEEDED) break ;;
    *) jq . "$EVIDENCE_DIRECTORY/smoke-execution.json" >&2; exit 1 ;;
  esac
done
test "$STATUS" = SUCCEEDED
SMOKE_ENDED_AT="$(date +%s)"
RUN_ID="$(jq -er '.output | fromjson | select(.status == "SUCCEEDED") | .run_id' "$EVIDENCE_DIRECTORY/smoke-execution.json")"
[[ "$RUN_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
```

Record the returned execution ARN, validated UUID, and bounded start/end times.
Do not fabricate a return value in documentation or source control.

```bash
export PUBLIC_API_BASE_URL="$(terraform -chdir=infra output -raw public_results_api_base_url)"
PRIVATE_SMOKE_HTTP_STATUS="$(curl --silent --show-error \
  --output "$EVIDENCE_DIRECTORY/private-smoke-response.json" \
  --write-out '%{http_code}' "${PUBLIC_API_BASE_URL}/runs/${RUN_ID}")"
test "$PRIVATE_SMOKE_HTTP_STATUS" = 404
```

## Results, checksums, and visibility

The service selects visibility only on the IAM-authorized submission. A PUBLIC
run is undiscoverable until finalization marks it SUCCEEDED. A terminal PRIVATE
record is not mutated after completion. The public API never changes visibility
and has no list or compute-start endpoint.

The PRIVATE smoke above must receive the uniform 404. A PUBLIC run is a
separate, separately approved paid submission with `visibility: PUBLIC`; it is
never a mutation of the private smoke and remains unevidenced here.

For an authorized operator, obtain artifact coordinates from the terminal run
record, then verify the exact six-object receipt against `checksums.json`; do
not guess object keys or make a bucket public. A successful public response can
contain the final summary and a five-minute presigned report URL. A private,
pending, failed, or unfinalized record remains unavailable through the public
route.

```bash
export ARTIFACT_BUCKET="$(terraform -chdir=infra output -raw artifact_bucket_name)"
export RESULT_PREFIX="runs/v1/${RUN_ID}/"
RESULT_RECEIPT_DIRECTORY="$(mktemp -d)"
trap 'rm -rf "$RESULT_RECEIPT_DIRECTORY"' EXIT
for artifact in run-spec.json result.json trades.parquet equity-curve.parquet report.html checksums.json; do
  aws s3 cp "s3://${ARTIFACT_BUCKET}/${RESULT_PREFIX}${artifact}" \
    "${RESULT_RECEIPT_DIRECTORY}/${artifact}"
done
(cd "$RESULT_RECEIPT_DIRECTORY" && jq -r '.artifacts[] | "\(.sha256)  \(.name)"' checksums.json | sha256sum --check)
```

## Logs and cost inspection

Use the actual output map rather than invented log names. The queries below are
read-only operational inspection after an approved smoke run.

```bash
terraform -chdir=infra output -json runtime_log_group_names
export LOG_GROUP="$(terraform -chdir=infra output -json runtime_log_group_names | jq -r '.step_functions')"
QUERY_ID="$(aws logs start-query --log-group-name "$LOG_GROUP" \
  --start-time "$SMOKE_STARTED_AT" --end-time "$SMOKE_ENDED_AT" \
  --query-string "fields @timestamp, @message | filter @message like /${RUN_ID}/ | sort @timestamp desc | limit 50" \
  --query queryId --output text)"
[[ "$QUERY_ID" =~ ^[0-9a-f-]{36}$ ]]
for _ in $(seq 1 30); do
  aws logs get-query-results --query-id "$QUERY_ID" > "$EVIDENCE_DIRECTORY/smoke-logs-query.json"
  QUERY_STATUS="$(jq -r .status "$EVIDENCE_DIRECTORY/smoke-logs-query.json")"
  case "$QUERY_STATUS" in
    Scheduled|Running) sleep 2 ;;
    Complete) break ;;
    *) jq . "$EVIDENCE_DIRECTORY/smoke-logs-query.json" >&2; exit 1 ;;
  esac
done
test "$QUERY_STATUS" = Complete

export COST_START='YYYY-MM-DD'  # approved smoke date, inclusive
export COST_END='YYYY-MM-DD'    # following date, exclusive
aws ce get-cost-and-usage --time-period Start="$COST_START",End="$COST_END" \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```

The AWS Budget (`budget_name`) alerts but does not stop spend. Check its
recipients before approval and record the Cost Explorer amount only after the
approved smoke run. Cost Explorer can lag billing data, so an immediate zero is
not final cost evidence; record the query window and revisit after AWS’s billing
data has settled.

## Disable, inventory, destroy, and retained-resource cleanup

Inventory first. Set the schedule disabled and save all runtime outputs before
any destructive operation. The artifact bucket has `force_destroy=false` and
the ECR repository has `force_delete=false`, so their versions/delete markers
and images must be inventoried and explicitly purged before a Terraform destroy
can succeed. Keep the bootstrap state bucket until application destruction and
the retained-resource inventory are verified.

```bash
set -euo pipefail
terraform -chdir=infra output schedule_enabled
terraform -chdir=infra plan -input=false -out=disable-schedule.tfplan \
  -var='enable_schedule=false' -var="image_digest=${IMAGE_DIGEST}"
terraform -chdir=infra show -json disable-schedule.tfplan > "$EVIDENCE_DIRECTORY/disable-schedule-plan.json"
jq -e '.planned_values.outputs.schedule_enabled.value == false' \
  "$EVIDENCE_DIRECTORY/disable-schedule-plan.json"
sha256sum infra/disable-schedule.tfplan | tee "$EVIDENCE_DIRECTORY/disable-schedule.tfplan.sha256"
terraform -chdir=infra show -no-color disable-schedule.tfplan
# After explicit approval only:
terraform -chdir=infra apply -input=false disable-schedule.tfplan
test "$(terraform -chdir=infra output -raw schedule_enabled)" = false

RUNTIME_OUTPUTS="$EVIDENCE_DIRECTORY/runtime-outputs.json"
terraform -chdir=infra output -json > "$RUNTIME_OUTPUTS"
ARTIFACT_BUCKET="$(jq -er '.artifact_bucket_name.value' "$RUNTIME_OUTPUTS")"
ECR_REPOSITORY_NAME="$(jq -er '.ecr_repository_name.value' "$RUNTIME_OUTPUTS")"
TASK_SUBNET_ID="$(jq -er '.task_subnet_id.value' "$RUNTIME_OUTPUTS")"
VPC_ID="$(jq -er '.vpc_id.value' "$RUNTIME_OUTPUTS")"
BUDGET_NAME="$(jq -er '.budget_name.value' "$RUNTIME_OUTPUTS")"
RUNTIME_LOG_GROUPS="$(jq -cer '.runtime_log_group_names.value | to_entries | map(.value)' "$RUNTIME_OUTPUTS")"
[[ "$ARTIFACT_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]
[[ "$ECR_REPOSITORY_NAME" =~ ^[a-z0-9][a-z0-9-]{1,254}$ ]]
[[ "$TASK_SUBNET_ID" =~ ^subnet-[0-9a-f]+$ && "$VPC_ID" =~ ^vpc-[0-9a-f]+$ ]]
jq -e 'length == 8 and all(.[]; type == "string" and (startswith("/aws/") or startswith("/ecs/")))' <<< "$RUNTIME_LOG_GROUPS"

aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Project,Values="$TF_VAR_project" Key=Environment,Values="$TF_VAR_environment"
aws s3api list-object-versions --bucket "$ARTIFACT_BUCKET" \
  --expected-bucket-owner "$ACCOUNT_ID"
# The following inventory covers ephemeral task public IPv4s by exact subnet/VPC.
aws ec2 describe-network-interfaces \
  --filters "Name=subnet-id,Values=${TASK_SUBNET_ID}" "Name=vpc-id,Values=${VPC_ID}" \
  --output json > "$EVIDENCE_DIRECTORY/enis.json"
SAVED_ENI_IDS="$(jq -cer '[.NetworkInterfaces[]?.NetworkInterfaceId] | unique' "$EVIDENCE_DIRECTORY/enis.json")"
aws ec2 describe-network-interfaces \
  --filters "Name=subnet-id,Values=${TASK_SUBNET_ID}" "Name=vpc-id,Values=${VPC_ID}" \
  --query 'NetworkInterfaces[].{NetworkInterfaceId:NetworkInterfaceId,Status:Status,PublicIp:Association.PublicIp,Description:Description}'
# This is a separate Elastic IP (EIP) inventory; it does not enumerate task public IPv4s.
aws ec2 describe-addresses \
  --filters "Name=tag:Project,Values=${TF_VAR_project}" "Name=tag:Environment,Values=${TF_VAR_environment}" \
  --output json > "$EVIDENCE_DIRECTORY/eips.json"
SAVED_EIP_ALLOCATION_IDS="$(jq -cer '[.Addresses[]?.AllocationId] | unique' "$EVIDENCE_DIRECTORY/eips.json")"
aws ecr list-images --repository-name "$ECR_REPOSITORY_NAME"
for LOG_INDEX in $(seq 0 7); do
  LOG_GROUP="$(jq -er ".[$LOG_INDEX]" <<< "$RUNTIME_LOG_GROUPS")"
  aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
    --output json > "$EVIDENCE_DIRECTORY/log-group-${LOG_INDEX}.json"
  jq -e --arg name "$LOG_GROUP" '[.logGroups[]?.logGroupName] | index($name) != null' \
    "$EVIDENCE_DIRECTORY/log-group-${LOG_INDEX}.json"
done
aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"
```

Before purging resources, archive the application backend state in the
organization-approved secure location outside this repository. State can contain
resource metadata and must never be committed. The archive names are unique and
no-clobber; `set -euo pipefail` plus JSON validation means a failed or truncated
state pull stops before any purge.

```bash
set -euo pipefail
umask 077
export APPROVED_STATE_ARCHIVE_DIRECTORY='/approved/secure/state-archive'
case "$APPROVED_STATE_ARCHIVE_DIRECTORY" in "$PWD"|"$PWD"/*) exit 2 ;; esac
mkdir -p "$APPROVED_STATE_ARCHIVE_DIRECTORY"
APPLICATION_ARCHIVE="$APPROVED_STATE_ARCHIVE_DIRECTORY/application-pre-destroy-$(date -u +%Y%m%d%H%M%S)-$$.tfstate"
set -o noclobber
terraform -chdir=infra state pull > "$APPLICATION_ARCHIVE"
chmod 600 "$APPLICATION_ARCHIVE"
jq -e 'type == "object" and has("version")' "$APPLICATION_ARCHIVE" >/dev/null
terraform -chdir=infra plan -destroy -input=false -out=runtime-destroy.tfplan \
  -var="image_digest=${IMAGE_DIGEST}"
sha256sum infra/runtime-destroy.tfplan | tee "$EVIDENCE_DIRECTORY/runtime-destroy.tfplan.sha256"
terraform -chdir=infra show -no-color runtime-destroy.tfplan
```

Obtain explicit cleanup approval tied to the exact runtime-destroy checksum
above. Only then purge the exact artifact-bucket object versions/delete markers
and exact ECR repository images, then apply that already-reviewed plan. These
bounded loops capture every deletion response, reject item-level failures, and
fail if the inventory does not drain within its attempt bound.

```bash
set -euo pipefail
for DELETE_ATTEMPT in $(seq 1 100); do
  S3_VERSIONS="$(aws s3api list-object-versions --bucket "$ARTIFACT_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --max-keys 1000 --output json)"
  S3_DELETE_PAYLOAD="$(jq -c '{Objects: (((.Versions // []) + (.DeleteMarkers // [])) | .[:1000] | map({Key, VersionId})), Quiet: true}' <<< "$S3_VERSIONS")"
  test "$(jq '.Objects | length' <<< "$S3_DELETE_PAYLOAD")" -gt 0 || break
  S3_DELETE_RESPONSE="$(aws s3api delete-objects --bucket "$ARTIFACT_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --delete "$S3_DELETE_PAYLOAD")"
  jq -e '(.Errors // []) | length == 0' <<< "$S3_DELETE_RESPONSE"
done
S3_REMAINING="$(aws s3api list-object-versions --bucket "$ARTIFACT_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --max-keys 1000 --output json)"
jq -e '((.Versions // []) + (.DeleteMarkers // []) | length) == 0' <<< "$S3_REMAINING"
for DELETE_ATTEMPT in $(seq 1 100); do
  ECR_IMAGE_IDS="$(aws ecr list-images --repository-name "$ECR_REPOSITORY_NAME" --max-items 100 --query imageIds --output json)"
  test "$(jq 'length' <<< "$ECR_IMAGE_IDS")" -gt 0 || break
  ECR_DELETE_RESPONSE="$(aws ecr batch-delete-image --repository-name "$ECR_REPOSITORY_NAME" --image-ids "$ECR_IMAGE_IDS")"
  jq -e '(.failures // []) | length == 0' <<< "$ECR_DELETE_RESPONSE"
done
ECR_REMAINING="$(aws ecr list-images --repository-name "$ECR_REPOSITORY_NAME" --max-items 100 --query imageIds --output json)"
jq -e 'length == 0' <<< "$ECR_REMAINING"
# After explicit approval only:
terraform -chdir=infra apply -input=false runtime-destroy.tfplan

# Terraform owns the managed log groups, budget, VPC/subnet, and security groups.
# Check for retained resources before any manual action; reconcile Terraform state first.
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=Project,Values="$TF_VAR_project" Key=Environment,Values="$TF_VAR_environment"
aws ec2 describe-network-interfaces \
  --filters "Name=subnet-id,Values=${TASK_SUBNET_ID}" "Name=vpc-id,Values=${VPC_ID}" \
  --query 'NetworkInterfaces[].{NetworkInterfaceId:NetworkInterfaceId,Status:Status,PublicIp:Association.PublicIp,Description:Description}'
aws ec2 describe-addresses \
  --filters "Name=tag:Project,Values=${TF_VAR_project}" "Name=tag:Environment,Values=${TF_VAR_environment}"
for LOG_INDEX in $(seq 0 7); do
  LOG_GROUP="$(jq -er ".[$LOG_INDEX]" <<< "$RUNTIME_LOG_GROUPS")"
  aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
    --output json > "$EVIDENCE_DIRECTORY/post-destroy-log-group-${LOG_INDEX}.json"
  jq -e --arg name "$LOG_GROUP" '[.logGroups[]?.logGroupName] | index($name) == null' \
    "$EVIDENCE_DIRECTORY/post-destroy-log-group-${LOG_INDEX}.json"
done
if S3_ABSENCE="$(aws s3api list-object-versions --bucket "$ARTIFACT_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" 2>&1)"; then
  printf '%s\n' "artifact bucket still exists: $S3_ABSENCE" >&2; exit 1
elif ! grep -q 'NoSuchBucket' <<< "$S3_ABSENCE"; then
  printf '%s\n' "unexpected artifact-bucket check failure: $S3_ABSENCE" >&2; exit 1
fi
if ECR_ABSENCE="$(aws ecr list-images --repository-name "$ECR_REPOSITORY_NAME" 2>&1)"; then
  printf '%s\n' "ECR repository still exists: $ECR_ABSENCE" >&2; exit 1
elif ! grep -q 'RepositoryNotFoundException' <<< "$ECR_ABSENCE"; then
  printf '%s\n' "unexpected ECR check failure: $ECR_ABSENCE" >&2; exit 1
fi
if BUDGET_ABSENCE="$(aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" 2>&1)"; then
  printf '%s\n' "budget still exists: $BUDGET_ABSENCE" >&2; exit 1
elif ! grep -q 'NotFoundException' <<< "$BUDGET_ABSENCE"; then
  printf '%s\n' "unexpected budget check failure: $BUDGET_ABSENCE" >&2; exit 1
fi
```

If a retained ENI, public IPv4 association, EIP, log group, or budget is proven
by the post-destroy inventory, stop for a separate cleanup approval and review
the Terraform state for drift before a tightly scoped provider command. Do not
delete Terraform-managed resources ad hoc and leave stale state behind. After
the reviewed drift resolution proves Terraform no longer owns the exact
leftover, use only the matching command below with IDs/names copied from the
post-destroy inventory:

```bash
# Set one exact kind and one identifier copied from the approved post-destroy inventory.
# An EIP requires association/ownership reconciliation and is intentionally escalated,
# not deleted by this runbook.
set -euo pipefail
export RETAINED_RESOURCE_KIND='log-group' # log-group, eni, or budget
export RETAINED_RESOURCE_ID='exact identifier from post-destroy inventory'
case "$RETAINED_RESOURCE_KIND" in
  log-group)
    jq -e --arg name "$RETAINED_RESOURCE_ID" 'index($name) != null' <<< "$RUNTIME_LOG_GROUPS"
    aws logs delete-log-group --log-group-name "$RETAINED_RESOURCE_ID"
    ;;
  eni)
    jq -e --arg id "$RETAINED_RESOURCE_ID" 'index($id) != null' <<< "$SAVED_ENI_IDS"
    aws ec2 delete-network-interface --network-interface-id "$RETAINED_RESOURCE_ID"
    ;;
  budget)
    test "$RETAINED_RESOURCE_ID" = "$BUDGET_NAME"
    aws budgets delete-budget --account-id "$ACCOUNT_ID" --budget-name "$RETAINED_RESOURCE_ID"
    ;;
  *) printf '%s\n' 'unsupported retained resource kind' >&2; exit 2 ;;
esac
```

The bootstrap OIDC provider, GitHub roles, runtime permission boundary, and
state bucket are a final, separate cleanup phase. Keep the state bucket until
runtime destruction and all retained checks pass; archive the state externally,
then inventory and purge its versions/delete markers only with separate
approval. `infra/bootstrap` uses local state because it is initialized with
`-backend=false`; archive `infra/bootstrap/terraform.tfstate` if it is present.
Also archive the final application backend state before removing its bucket.
`prevent_destroy` is intentional: a reviewed Terraform change must remove that
lifecycle protection before a saved bootstrap destroy plan can delete the state
bucket. Do not bypass it with an ad-hoc command.

```bash
set -euo pipefail
[[ "$TF_STATE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]
umask 077
export APPROVED_STATE_ARCHIVE_DIRECTORY='/approved/secure/state-archive'
case "$APPROVED_STATE_ARCHIVE_DIRECTORY" in "$PWD"|"$PWD"/*) exit 2 ;; esac
mkdir -p "$APPROVED_STATE_ARCHIVE_DIRECTORY"
set -o noclobber
if test -f infra/bootstrap/terraform.tfstate; then
  BOOTSTRAP_ARCHIVE="$APPROVED_STATE_ARCHIVE_DIRECTORY/bootstrap-local-$(date -u +%Y%m%d%H%M%S)-$$.tfstate"
  cat infra/bootstrap/terraform.tfstate > "$BOOTSTRAP_ARCHIVE"
  chmod 600 "$BOOTSTRAP_ARCHIVE"
  jq -e 'type == "object" and has("version")' "$BOOTSTRAP_ARCHIVE" >/dev/null
fi
APPLICATION_FINAL_ARCHIVE="$APPROVED_STATE_ARCHIVE_DIRECTORY/application-final-$(date -u +%Y%m%d%H%M%S)-$$.tfstate"
terraform -chdir=infra state pull > "$APPLICATION_FINAL_ARCHIVE"
chmod 600 "$APPLICATION_FINAL_ARCHIVE"
jq -e 'type == "object" and has("version")' "$APPLICATION_FINAL_ARCHIVE" >/dev/null

# After the reviewed removal of prevent_destroy, make the exact plan before any purge.
terraform -chdir=infra/bootstrap plan -destroy -input=false -out=bootstrap-destroy.tfplan
sha256sum infra/bootstrap/bootstrap-destroy.tfplan | tee "$EVIDENCE_DIRECTORY/bootstrap-destroy.tfplan.sha256"
terraform -chdir=infra/bootstrap show -no-color bootstrap-destroy.tfplan
# Obtain separate explicit approval tied to this bootstrap-destroy checksum before continuing.
aws s3api list-object-versions --bucket "$TF_STATE_BUCKET" \
  --expected-bucket-owner "$ACCOUNT_ID"
# Only after the separate state-retention and cleanup approval tied to that checksum:
for DELETE_ATTEMPT in $(seq 1 100); do
  STATE_VERSIONS="$(aws s3api list-object-versions --bucket "$TF_STATE_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --max-keys 1000 --output json)"
  STATE_DELETE_PAYLOAD="$(jq -c '{Objects: (((.Versions // []) + (.DeleteMarkers // [])) | .[:1000] | map({Key, VersionId})), Quiet: true}' <<< "$STATE_VERSIONS")"
  test "$(jq '.Objects | length' <<< "$STATE_DELETE_PAYLOAD")" -gt 0 || break
  STATE_DELETE_RESPONSE="$(aws s3api delete-objects --bucket "$TF_STATE_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --delete "$STATE_DELETE_PAYLOAD")"
  jq -e '(.Errors // []) | length == 0' <<< "$STATE_DELETE_RESPONSE"
done
STATE_REMAINING="$(aws s3api list-object-versions --bucket "$TF_STATE_BUCKET" --expected-bucket-owner "$ACCOUNT_ID" --max-keys 1000 --output json)"
jq -e '((.Versions // []) + (.DeleteMarkers // []) | length) == 0' <<< "$STATE_REMAINING"
terraform -chdir=infra/bootstrap apply -input=false bootstrap-destroy.tfplan
```

## Resume-quality evidence

| Evidence item | Value to record after approved smoke run |
| --- | --- |
| Image digest and ECR repository | record after approved smoke run |
| State-machine execution ARN and run ID | record after approved smoke run |
| Dataset and result SHA-256 values | record after approved smoke run |
| Start/end timestamps and duration | record after approved smoke run |
| Each artifact byte size and `checksums.json` receipt | record after approved smoke run |
| CloudWatch Logs Insights query ID and result summary | record after approved smoke run |
| Cost Explorer time window, currency, and amount | record after approved smoke run |
| Budget name/recipients and schedule-disabled output | record after approved smoke run |
| Cleanup owner, date, inventory, and destroy-plan checksum | record after approved smoke run |

This table intentionally contains no claimed live measurement. Retain the
approved plan, checksum, reviewer decision, environment REST evidence, smoke
request, and cleanup evidence outside source control according to the team’s
records policy.
