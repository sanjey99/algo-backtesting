"""Static security contracts for the manually dispatched AWS workflows."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")
IAM_CONFIGURATION = Path("infra/iam.tf")
VARIABLES_CONFIGURATION = Path("infra/variables.tf")
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


def _assert_safe_action_pins(document: dict[str, object]) -> None:
    for reference in _all_action_references(document):
        assert ACTION_SHA.fullmatch(reference), reference


def _assert_no_static_keys_or_untrusted_execution(text: str) -> None:
    lowered = text.lower()
    assert "pull_request_target" not in lowered
    assert not any(marker in lowered for marker in STATIC_AWS_KEY_MARKERS)
    assert "actions/download-artifact" not in lowered
    assert "workflow_run" not in lowered


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
                            resource_match.group(1).strip(),
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
    assert "environment: aws-demo" in text
    assert '-backend-config="use_lockfile=true"' in text
    assert "TF_VAR_backend_state_key: ${{ vars.TF_STATE_KEY }}" in text
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
    assert "aws ecr describe-images" in text
    assert "image_digest" in text
    assert "tflint --chdir=infra --recursive" in text
    assert "terraform -chdir=infra test" in text
    assert "make test" in text
    assert "make verify-warnings" in text
    assert "make lint" in text
    assert _smoke_request(text) == {
        "schema_version": "1",
        "symbol": "SPY",
        "start": "2024-01-02",
        "end": "2024-01-10",
        "strategy_key": "ma_crossover",
        "strategy_parameters": {"fast_window": 10, "slow_window": 20},
        "initial_capital": 10000.0,
        "commission_pct": 0.001,
        "slippage_pct": 0.0005,
        "visibility": "PRIVATE",
    }
    _assert_safe_action_pins(workflow)
    _assert_no_static_keys_or_untrusted_execution(text)


def test_oidc_roles_have_separate_exact_trust_subjects_and_capabilities() -> None:
    iam = IAM_CONFIGURATION.read_text(encoding="utf-8")
    plan_policy = iam.split('resource "aws_iam_role_policy" "github_plan"', 1)[1].split(
        'resource "aws_iam_role_policy" "github_deploy"', 1
    )[0]
    deploy_policy = iam.split('resource "aws_iam_role_policy" "github_deploy"', 1)[1]

    assert 'url             = "https://token.actions.githubusercontent.com"' in iam
    assert 'values   = ["repo:${var.github_repository}:ref:${var.deploy_ref}"]' in iam
    assert (
        'values   = ["repo:${var.github_repository}:environment:${var.deploy_environment}"]'
        in iam
    )
    assert "states:StartExecution" not in plan_policy
    assert "ecr:PutImage" not in plan_policy
    assert "ecr:GetAuthorizationToken" not in plan_policy
    assert 'Action = ["*"]' not in plan_policy
    assert '"aws:RequestedRegion" = var.region' in plan_policy
    plan_matrix = _iam_statement_matrix(plan_policy)
    assert (
        frozenset({"iam:ListOpenIDConnectProviders", "iam:ListRoles", "sts:GetCallerIdentity"}),
        '"*"',
    ) in plan_matrix
    assert (frozenset({"budgets:Describe*"}), "local.github_budget_arn") in plan_matrix
    assert (frozenset({"s3:GetObject"}), "local.github_state_object_arn") in plan_matrix
    assert (
        frozenset({"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}),
        "local.github_state_lock_arn",
    ) in plan_matrix
    assert not any(
        {"s3:PutObject", "s3:DeleteObject"} & actions
        and resource == "local.github_state_object_arn"
        for actions, resource in plan_matrix
    )
    assert not any("tfstate-*/*" in resource for _actions, resource in plan_matrix)
    assert not any(
        {"ecr:GetAuthorizationToken", "ecr:PutImage", "states:StartExecution"} & actions
        for actions, _resource in plan_matrix
    )

    deploy_matrix = _iam_statement_matrix(deploy_policy)
    assert (
        frozenset({"iam:ListOpenIDConnectProviders", "iam:ListRoles", "sts:GetCallerIdentity"}),
        '"*"',
    ) in deploy_matrix
    assert any(
        actions
        == frozenset(
            {
                "budgets:CreateBudget",
                "budgets:ModifyBudget",
                "budgets:DeleteBudget",
                "budgets:Describe*",
            }
        )
        and resource == "local.github_budget_arn"
        for actions, resource in deploy_matrix
    )
    assert any(
        "events:PutRule" in actions and resource == "local.github_event_rule_arn_pattern"
        for actions, resource in deploy_matrix
    )
    assert (
        frozenset({"s3:GetObject", "s3:PutObject"}),
        "local.github_state_object_arn",
    ) in deploy_matrix
    assert (
        frozenset({"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}),
        "local.github_state_lock_arn",
    ) in deploy_matrix
    assert not any("tfstate-*/*" in resource for _actions, resource in deploy_matrix)
    assert "states:StartExecution" in deploy_policy
    assert "AdministratorAccess" not in deploy_policy
    assert 'resource "aws_iam_openid_connect_provider" "github_actions"' in iam
    assert "iam:CreateOpenIDConnectProvider" in deploy_policy
    assert "iam:DeleteOpenIDConnectProvider" in deploy_policy
    assert "iam:UpdateOpenIDConnectProviderThumbprint" in deploy_policy
    assert "lambda:PutFunctionConcurrency" in deploy_policy
    assert "dynamodb:UpdateTimeToLive" in deploy_policy
    assert "dynamodb:UpdateContinuousBackups" in deploy_policy
    assert "ec2:ModifyVpcAttribute" in deploy_policy
    assert "events:TagResource" in deploy_policy
    assert "ecr:SetRepositoryPolicy" in deploy_policy
    assert "aws:RequestTag/Project" not in deploy_policy
    assert "aws:ResourceTag/Environment" not in deploy_policy
    assert deploy_policy.count('"aws:RequestedRegion" = var.region') >= 3
    variables = VARIABLES_CONFIGURATION.read_text(encoding="utf-8")
    assert 'variable "backend_state_key"' in variables
    assert 'default     = "terraform.tfstate"' in variables
    assert '!strcontains(var.backend_state_key, "..")' in variables


def test_deploy_policy_is_bootstrapable_from_deterministic_resource_patterns() -> None:
    iam = IAM_CONFIGURATION.read_text(encoding="utf-8")
    deploy_policy = iam.split('resource "aws_iam_role_policy" "github_deploy"', 1)[1]

    live_resource_references = (
        "aws_s3_bucket.", "aws_dynamodb_table.", "aws_ecr_repository.",
        "aws_lambda_function.", "aws_ecs_cluster.", "aws_cloudwatch_event_rule.",
        "aws_cloudwatch_log_group.", "aws_scheduler_schedule.", "aws_sfn_state_machine.",
    )
    assert not any(reference in deploy_policy for reference in live_resource_references)
    assert "iam:Get*" not in deploy_policy
    assert "iam:List*" not in deploy_policy
    assert "Resource = local.github_budget_arn" in deploy_policy
    for local_name in (
        "github_artifact_bucket_arn",
        "github_lambda_arn_pattern",
        "github_runs_table_arn",
        "github_worker_repository_arn",
        "github_ecs_cluster_arn",
        "github_state_machine_arn",
        "github_event_rule_arn_pattern",
        "github_log_group_arn_patterns",
    ):
        assert f"Resource = local.{local_name}" in deploy_policy

    for deterministic_pattern in (
        "${local.name_prefix}-monthly-guardrail",
        "s3:::${local.name_prefix}-art-*",
        "function:${local.name_prefix}-*",
        "table/${local.name_prefix}-runs",
        "repository/${local.name_prefix}-worker",
        "cluster/${local.name_prefix}-research",
        "stateMachine:${local.name_prefix}-research",
        "rule/${local.name_prefix}-ecs-worker-*",
    ):
        assert deterministic_pattern in iam
