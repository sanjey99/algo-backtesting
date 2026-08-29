"""Static security contracts for the manually dispatched AWS workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")
IAM_CONFIGURATION = Path("infra/iam.tf")
VARIABLES_CONFIGURATION = Path("infra/variables.tf")
BOOTSTRAP_CONFIGURATION = Path("infra/bootstrap/iam.tf")
BOOTSTRAP_VARIABLES = Path("infra/bootstrap/variables.tf")
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}(?:\s+#\s+v\d[\w.\-]*)?$")
STATIC_AWS_KEY_MARKERS = ("aws_access_key_id", "aws_secret_access_key")


def _workflow(name: str) -> tuple[dict[str, object], str]:
    path = WORKFLOWS / name
    text = path.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document, text


def _all_action_references(document: object) -> list[str]:
    if isinstance(document, dict):
        values = [document.get("uses")]
        return [
            value
            for child in document.values()
            for value in _all_action_references(child)
        ] + [value for value in values if isinstance(value, str)]
    if isinstance(document, list):
        return [value for child in document for value in _all_action_references(child)]
    return []


def _setup_terraform_versions(document: object) -> list[str]:
    """Return every parsed hashicorp/setup-terraform version pin."""
    if isinstance(document, dict):
        version: list[str] = []
        uses = document.get("uses")
        if isinstance(uses, str) and uses.startswith("hashicorp/setup-terraform@"):
            configuration = document.get("with")
            assert isinstance(configuration, dict)
            terraform_version = configuration.get("terraform_version")
            assert isinstance(terraform_version, str)
            version.append(terraform_version)
        return version + [
            configured_version
            for child in document.values()
            for configured_version in _setup_terraform_versions(child)
        ]
    if isinstance(document, list):
        return [version for child in document for version in _setup_terraform_versions(child)]
    return []


def _assert_safe_action_pins(document: dict[str, object]) -> None:
    for reference in _all_action_references(document):
        assert ACTION_SHA.fullmatch(reference), reference


def _assert_no_static_keys_or_untrusted_execution(
    text: str, *, allow_trusted_artifact_transfer: bool = False
) -> None:
    lowered = text.lower()
    assert "pull_request_target" not in lowered
    assert not any(marker in lowered for marker in STATIC_AWS_KEY_MARKERS)
    if not allow_trusted_artifact_transfer:
        assert "actions/download-artifact" not in lowered
    assert "\n  workflow_run:" not in lowered


def _smoke_request(text: str) -> dict[str, object]:
    match = re.search(r"--input '([^']+)'", text)
    assert match is not None
    request = json.loads(match.group(1))
    assert isinstance(request, dict)
    return request


def _iam_statement_matrix(policy: str) -> list[tuple[frozenset[str], str]]:
    """Extract action/resource pairs from a Terraform jsonencode policy block."""
    policy = re.sub(r"\$\{[^}]+\}", "INTERPOLATION", policy)
    statement_start = policy.index("Statement = [")
    statements: list[tuple[frozenset[str], str]] = []
    depth = 0
    block_start: int | None = None
    for index, character in enumerate(policy[statement_start:], start=statement_start):
        if character == "{":
            if depth == 0:
                block_start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0 and block_start is not None:
                block = policy[block_start : index + 1]
                action_match = re.search(r"Action\s*=\s*\[(.*?)\]", block, re.DOTALL)
                resource_match = re.search(r"Resource\s*=\s*([^\n]+)", block)
                if action_match and resource_match:
                    statements.append(
                        (
                            frozenset(re.findall(r'"([^"]+)"', action_match.group(1))),
                            resource_match.group(1).strip().rstrip(" }"),
                        )
                    )
    return statements


def test_checks_is_pr_safe_and_has_no_aws_identity() -> None:
    workflow, text = _workflow("aws-checks.yml")

    assert set(workflow["on"]) == {"pull_request", "push"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "id-token" not in text
    assert "configure-aws-credentials" not in text
    assert "docker build" in text
    assert "docker push" not in text
    assert "uv sync --locked --extra dev --extra cloud" in text
    assert "uv lock --check" in text
    assert "pip-audit" in text
    assert "--extra cloud" in text
    assert "terraform fmt -check -recursive infra" in text
    assert "terraform -chdir=infra validate" in text
    assert "terraform -chdir=infra test" in text
    assert "tflint --chdir=infra --recursive" in text
    assert "terraform -chdir=infra test" in text
    assert "make test" in text
    assert "make verify-warnings" in text
    assert "make lint" in text
    assert "checkov" in text
    _assert_safe_action_pins(workflow)
    _assert_no_static_keys_or_untrusted_execution(text)


def test_aws_workflows_pin_all_setup_terraform_steps_to_stable_version() -> None:
    """Terraform test syntax needs the stable version that supports this repository."""
    for workflow_name in ("aws-checks.yml", "aws-plan.yml", "aws-deploy.yml"):
        workflow, _ = _workflow(workflow_name)
        versions = _setup_terraform_versions(workflow)
        assert versions
        assert set(versions) == {"1.15.9"}


def test_ci_installs_and_audits_cloud_dependencies_before_the_full_suite() -> None:
    """Removing the cloud extra would make CI fail while collecting cloud tests."""
    workflow, _ = _workflow("ci.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)

    for job_name in ("linux-quality", "windows-quality"):
        job = jobs[job_name]
        assert isinstance(job, dict)
        steps = job["steps"]
        assert isinstance(steps, list)
        install_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Install locked dependencies"
        )
        full_suite_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Run full test suite with coverage"
        )
        install_step = steps[install_index]
        assert isinstance(install_step, dict)
        assert re.findall(r"--extra\s+(\w+)", install_step["run"]) == ["dev", "cloud"]
        assert install_index < full_suite_index

    linux_steps = jobs["linux-quality"]["steps"]
    assert isinstance(linux_steps, list)
    audit_step = next(
        step for step in linux_steps if step.get("name") == "Audit locked dependencies"
    )
    assert isinstance(audit_step, dict)
    export = re.search(r"uv export[^\n]+", audit_step["run"])
    assert export is not None
    assert re.findall(r"--extra\s+(\w+)", export.group()) == ["dev", "cloud"]


def test_plan_is_manual_read_only_and_does_not_publish_sensitive_artifacts() -> None:
    workflow, text = _workflow("aws-plan.yml")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert "AWS_PLAN_ROLE_ARN" in text
    assert "terraform -chdir=infra plan -input=false -out=tfplan" in text
    assert "terraform -chdir=infra show -no-color tfplan" in text
    assert "terraform apply" not in text
    assert "upload-artifact" not in text
    assert "terraform.tfstate" not in text
    assert "terraform -chdir=infra init -input=false" in text
    assert '-backend-config="use_lockfile=true"' in text
    assert "TF_VAR_backend_state_key: ${{ vars.TF_STATE_KEY }}" in text
    assert "TF_VAR_region: ${{ vars.AWS_REGION }}" in text
    assert '"$TF_STATE_KEY" != *".."*' in text
    assert "tflint --chdir=infra --recursive" in text
    assert "terraform -chdir=infra test" in text
    assert "make test" in text
    assert "make verify-warnings" in text
    assert "make lint" in text
    assert "PLAN_REASON" in text
    assert '"${{ inputs.reason }}"' not in text
    _assert_safe_action_pins(workflow)
    _assert_no_static_keys_or_untrusted_execution(text)


def test_deploy_is_manual_protected_and_applies_only_saved_plans() -> None:
    workflow, text = _workflow("aws-deploy.yml")

    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert "concurrency:" in text
    assert "foundation-plan" in workflow["jobs"]
    assert "foundation-apply" in workflow["jobs"]
    assert "preflight" in workflow["jobs"]
    assert "runtime-plan" in workflow["jobs"]
    assert "runtime-apply" in workflow["jobs"]
    assert workflow["jobs"]["foundation-apply"]["needs"] == ["foundation-plan"]
    assert workflow["jobs"]["runtime-apply"]["needs"] == ["runtime-plan"]
    assert workflow["jobs"]["foundation-apply"]["environment"] == "aws-demo"
    assert workflow["jobs"]["runtime-plan"]["environment"] == "aws-demo"
    assert workflow["jobs"]["runtime-apply"]["environment"] == "aws-demo"
    assert '-backend-config="use_lockfile=true"' in text
    assert "TF_VAR_backend_state_key: ${{ vars.TF_STATE_KEY }}" in text
    assert "TF_VAR_region: ${{ vars.AWS_REGION }}" in text
    assert '"$TF_STATE_KEY" != *".."*' in text
    assert "AWS_DEPLOY_ROLE_ARN" in text
    assert "execute_workflow" in text
    assert "default: false" in text
    foundation_plan = "terraform -chdir=infra plan -input=false -out=foundation.tfplan"
    foundation_apply = "terraform -chdir=infra apply -input=false -auto-approve foundation.tfplan"
    runtime_plan = "terraform -chdir=infra plan -input=false -out=runtime.tfplan"
    runtime_apply = "terraform -chdir=infra apply -input=false -auto-approve runtime.tfplan"
    assert foundation_plan in text
    assert foundation_apply in text
    assert runtime_plan in text
    assert runtime_apply in text
    assert text.index(foundation_plan) < text.index(
        foundation_apply
    )
    assert text.index(runtime_plan) < text.index(
        runtime_apply
    )
    assert "actions/upload-artifact" in text
    assert "actions/download-artifact" in text
    assert "retention-days: 1" in text
    assert "github.sha" in text
    assert "github.run_id" in text
    assert "github.run_attempt" in text
    assert "sha256sum" in text
    assert "manifest.json" in text
    assert "terraform -chdir=infra plan" in text
    assert "aws ecr describe-images" in text
    assert "docker push" in text
    assert 'plan -input=false -out=runtime.tfplan -var="image_digest=$IMAGE_DIGEST"' in text
    assert "if: ${{ inputs.execute_workflow }}" in text
    assert text.index("docker push") < text.index("-out=runtime.tfplan")
    assert text.index("runtime.tfplan") < text.index("start-execution")
    assert workflow["concurrency"]["group"] == "aws-deploy-${{ github.repository }}-aws-demo"
    assert workflow["jobs"]["foundation-plan"]["needs"] == ["preflight"]
    assert workflow["jobs"]["runtime-plan"]["needs"] == ["preflight", "foundation-apply"]
    runtime_condition = workflow["jobs"]["runtime-plan"]["if"]
    assert "needs.preflight.result == 'success'" in runtime_condition
    assert "inputs.first_deployment == false" in runtime_condition
    assert "needs.foundation-apply.result == 'success'" in runtime_condition
    upload_paths = [
        step["with"]["path"].split()
        for job in (workflow["jobs"]["foundation-plan"], workflow["jobs"]["runtime-plan"])
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert upload_paths == [
        ["infra/foundation.tfplan", "infra/foundation.tfplan.sha256", "infra/manifest.json"],
        ["infra/runtime.tfplan", "infra/runtime.tfplan.sha256", "infra/manifest.json"],
    ]
    download_steps = [
        step
        for job in (workflow["jobs"]["foundation-apply"], workflow["jobs"]["runtime-apply"])
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/download-artifact@")
    ]
    assert len(download_steps) == 2
    assert all(step["with"]["path"] == "infra" for step in download_steps)
    assert all(step["with"]["merge-multiple"] == "true" for step in download_steps)
    assert "infra/.terraform" not in text
    assert "terraform.tfstate" not in text
    assert "(cd infra && sha256sum foundation.tfplan > foundation.tfplan.sha256)" in text
    assert "(cd infra && sha256sum runtime.tfplan > runtime.tfplan.sha256)" in text
    assert text.count('aws sts get-caller-identity --query Account --output text') >= 4
    for manifest_binding in ("account_id", "region", "state_bucket", "state_key"):
        assert text.count(f"--arg {manifest_binding}") == 2
        assert text.count(f". {manifest_binding}") == 0
        assert text.count(f".{manifest_binding} infra/manifest.json") == 2
    preflight = workflow["jobs"]["preflight"]
    assert preflight["permissions"] == {"contents": "read"}
    for command in (
        "uv sync --locked --extra dev --extra cloud",
        "make test",
        "make verify-warnings",
        "make lint",
        "terraform fmt -check -recursive infra",
        "terraform -chdir=infra validate",
        "terraform -chdir=infra test",
        "tflint --chdir=infra --recursive",
        "checkov",
    ):
        assert command in text
    _assert_safe_action_pins(workflow)
    _assert_no_static_keys_or_untrusted_execution(text, allow_trusted_artifact_transfer=True)


def test_oidc_roles_have_separate_exact_trust_subjects_and_capabilities() -> None:
    iam = IAM_CONFIGURATION.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP_CONFIGURATION.read_text(encoding="utf-8")
    plan_policy = bootstrap.split('resource "aws_iam_role_policy" "github_plan"', 1)[1].split(
        'resource "aws_iam_role_policy" "github_deploy"', 1
    )[0]
    deploy_policy = bootstrap.split('resource "aws_iam_role_policy" "github_deploy"', 1)[1]

    assert 'url             = "https://token.actions.githubusercontent.com"' in bootstrap
    assert 'values   = ["repo:${var.github_repository}:ref:${var.deploy_ref}"]' in bootstrap
    assert (
        'values   = ["repo:${var.github_repository}:environment:${var.deploy_environment}"]'
        in bootstrap
    )
    assert "states:StartExecution" not in plan_policy
    assert "ecr:PutImage" not in plan_policy
    assert "ecr:GetAuthorizationToken" not in plan_policy
    assert 'Action = ["*"]' not in plan_policy
    assert '"aws:RequestedRegion" = var.region' in plan_policy
    plan_matrix = _iam_statement_matrix(plan_policy)
    assert (frozenset({"sts:GetCallerIdentity"}), '"*"') in plan_matrix
    assert (frozenset({"budgets:DescribeBudget"}), "local.github_budget_arn") in plan_matrix
    assert (frozenset({"s3:GetObject"}), "local.state_object_arn") in plan_matrix
    assert (
        frozenset({"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}),
        "local.state_lock_arn",
    ) in plan_matrix
    assert not any(
        {"s3:PutObject", "s3:DeleteObject"} & actions
        and resource == "local.state_object_arn"
        for actions, resource in plan_matrix
    )
    assert not any("tfstate-*/*" in resource for _actions, resource in plan_matrix)
    assert not any(
        {"ecr:GetAuthorizationToken", "ecr:PutImage", "states:StartExecution"} & actions
        for actions, _resource in plan_matrix
    )

    deploy_matrix = _iam_statement_matrix(deploy_policy)
    assert (frozenset({"sts:GetCallerIdentity"}), '"*"') in deploy_matrix
    assert any(
        actions
        == frozenset(
            {
                    "budgets:CreateBudget",
                    "budgets:ModifyBudget",
                    "budgets:DeleteBudget",
                    "budgets:DescribeBudget",
            }
        )
        and resource == "local.github_budget_arn"
        for actions, resource in deploy_matrix
    )
    assert any(
        actions == frozenset({"iam:CreateRole"}) for actions, _resource in deploy_matrix
    )
    assert '"iam:PermissionsBoundary" = local.runtime_permissions_boundary' in deploy_policy
    assert any(
        actions == frozenset({"iam:PassRole"}) for actions, _resource in deploy_matrix
    )
    assert (
        frozenset({"s3:GetObject", "s3:PutObject"}),
        "local.state_object_arn",
    ) in deploy_matrix
    assert (
        frozenset({"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}),
        "local.state_lock_arn",
    ) in deploy_matrix
    assert not any("tfstate-*/*" in resource for _actions, resource in deploy_matrix)
    assert "AdministratorAccess" not in deploy_policy
    assert 'resource "aws_iam_openid_connect_provider" "github_actions"' in bootstrap
    assert "iam:CreateOpenIDConnectProvider" not in deploy_policy
    assert "iam:DeleteOpenIDConnectProvider" not in deploy_policy
    assert "iam:UpdateOpenIDConnectProviderThumbprint" not in deploy_policy
    assert "aws:RequestTag/Project" not in deploy_policy
    assert "aws:ResourceTag/Environment" not in deploy_policy
    assert "iam:ListOpenIDConnectProviders" not in deploy_policy
    assert "iam:ListRoles" not in deploy_policy
    assert deploy_policy.count('"aws:RequestedRegion" = var.region') >= 1
    variables = BOOTSTRAP_VARIABLES.read_text(encoding="utf-8")
    assert 'variable "backend_state_key"' in variables
    assert 'default     = "terraform.tfstate"' in variables
    assert '!strcontains(var.backend_state_key, "..")' in variables
    assert 'regex("^[A-Za-z0-9][A-Za-z0-9._/-]{1,510}[A-Za-z0-9]$"' in variables
    assert 'resource "aws_iam_role" "github_deploy"' not in iam
    assert 'resource "aws_iam_role_policy" "github_deploy"' not in iam


def test_deploy_policy_is_bootstrapable_from_deterministic_resource_patterns() -> None:
    bootstrap = BOOTSTRAP_CONFIGURATION.read_text(encoding="utf-8")
    deploy_policy = bootstrap.split('resource "aws_iam_role_policy" "github_deploy"', 1)[1]

    live_resource_references = (
        "aws_s3_bucket.artifacts.", "aws_dynamodb_table.", "aws_ecr_repository.",
        "aws_lambda_function.", "aws_ecs_cluster.", "aws_cloudwatch_event_rule.",
        "aws_cloudwatch_log_group.", "aws_scheduler_schedule.", "aws_sfn_state_machine.",
    )
    assert not any(reference in deploy_policy for reference in live_resource_references)
    assert "aws_iam_openid_connect_provider.github_actions" not in deploy_policy
    assert "local.runtime_role_arns" in deploy_policy
    assert "iam:PermissionsBoundary" in deploy_policy
    assert "local.runtime_permissions_boundary" in deploy_policy
    assert "github-plan" not in deploy_policy
    assert "github-deploy" not in deploy_policy


def test_bootstrap_policies_use_an_explicit_provider_action_matrix() -> None:
    """Terraform's AWS provider surface must never be authorized by action globs."""
    bootstrap = BOOTSTRAP_CONFIGURATION.read_text(encoding="utf-8")
    deploy_policy = bootstrap.split('resource "aws_iam_role_policy" "github_deploy"', 1)[1]
    plan_policy = bootstrap.split('resource "aws_iam_role_policy" "github_plan"', 1)[1].split(
        'resource "aws_iam_role_policy" "github_deploy"', 1
    )[0]

    for policy in (plan_policy, deploy_policy):
        assert not re.search(
            r'"(?:[a-z0-9-]+):(?>Get|List|Describe|Create|Delete|Put|Update)\*"',
            policy,
        )

    for action in (
        "s3:GetEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:PutEncryptionConfiguration",
        "s3:PutLifecycleConfiguration",
        "s3:DeletePublicAccessBlock",
        "ecr:PutImageTagMutability",
        "ecr:PutImageScanningConfiguration",
        "ecr:DescribeImages",
        "lambda:PutFunctionConcurrency",
        "dynamodb:UpdateContinuousBackups",
        "ec2:ModifyVpcAttribute",
        "events:PutTargets",
        "scheduler:UpdateSchedule",
        "states:UpdateStateMachine",
        "states:StartExecution",
        "iam:ListAttachedRolePolicies",
        "iam:ListRoleTags",
        "iam:ListInstanceProfilesForRole",
        "iam:PutRolePermissionsBoundary",
        "ec2:DescribeVpcAttribute",
    ):
        assert action in deploy_policy
    for invalid_action in (
        "s3:GetBucketEncryption",
        "s3:GetBucketLifecycleConfiguration",
        "s3:DeleteBucketEncryption",
        "s3:DeleteBucketLifecycle",
        "s3:PutBucketEncryption",
        "s3:PutBucketLifecycleConfiguration",
    ):
        assert invalid_action not in bootstrap
    assert "local.cloudwatch_alarm_arns" in deploy_policy
    assert (
        'Action = ["cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", '
        '"cloudwatch:ListTagsForResource", "cloudwatch:PutMetricAlarm", '
        '"cloudwatch:TagResource", "cloudwatch:UntagResource"], '
        "Resource = local.cloudwatch_alarm_arns"
    ) in deploy_policy
    assert '"logs:CreateLogGroup"], Resource = "*"' not in deploy_policy
    assert '"logs:CreateLogGroup"' in deploy_policy
    assert "Resource = local.runtime_log_group_arns" in deploy_policy
    assert 'Resource = local.cloudwatch_alarm_arns' in deploy_policy
    assert '"iam:PermissionsBoundary" = local.runtime_permissions_boundary' in deploy_policy
    assert "iam:DeleteRolePermissionsBoundary" not in deploy_policy
    assert "StringLike = { \"s3:prefix\"" not in plan_policy
    assert "StringLike = { \"s3:prefix\"" not in deploy_policy
    assert plan_policy.count('StringEquals = { "s3:prefix"') == 1
    assert deploy_policy.count('StringEquals = { "s3:prefix"') == 1
    assert re.search(
        r'Action = \[[^\]]+"s3:ListBucket"[^\]]*\], Resource = local\.artifact_bucket_arn',
        plan_policy,
    )
    assert (
        'Action = ["logs:ListTagsForResource"], Resource = local.runtime_log_group_arns'
        in plan_policy
    )
    assert (
        'Action = ["cloudwatch:ListTagsForResource"], Resource = local.cloudwatch_alarm_arns'
        in plan_policy
    )
    assert '"iam:PutRolePermissionsBoundary"], Resource = local.runtime_role_arns' in deploy_policy


def test_runtime_boundary_admits_only_runtime_policy_resources() -> None:
    bootstrap = BOOTSTRAP_CONFIGURATION.read_text(encoding="utf-8")
    boundary = bootstrap.split(
        'resource "aws_iam_policy" "runtime_permissions_boundary"', 1
    )[1].split(
        'resource "aws_iam_openid_connect_provider"', 1
    )[0]

    for action in (
        "s3:GetObject",
        "s3:PutObject",
        "dynamodb:UpdateItem",
        "ecr:BatchGetImage",
        "lambda:InvokeFunction",
        "ecs:RunTask",
        "events:PutTargets",
        "states:StartExecution",
        "iam:PassRole",
    ):
        assert action in boundary
    assert 'Resource = "*"' not in boundary.split("# Unscopable", 1)[0]
    assert "local.artifact_object_arns" in boundary
    assert "local.runs_table_arn" in boundary
    assert "local.worker_repository_arn" in boundary
    assert "local.runtime_role_arns" in boundary
