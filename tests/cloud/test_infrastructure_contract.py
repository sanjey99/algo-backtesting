"""Offline contracts for the bounded AWS runtime Terraform surface.

The checks intentionally inspect the Terraform-owned JSON boundaries rather than
requiring an AWS account or a live plan.  Terraform's `jsonencode` definitions
remain the source rendered by the provider at apply time.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"


def _source(name: str) -> str:
    return (INFRA / name).read_text(encoding="utf-8")


def test_state_machine_is_one_bounded_fargate_run_with_closed_failure_paths() -> None:
    source = _source("step_functions.tf")

    assert "research_state_machine_definition = jsonencode" in source
    assert "definition = local.research_state_machine_definition" in source
    assert '"arn:aws:states:::ecs:runTask.sync"' in source
    assert source.count("ecs:runTask.sync") == 1
    assert "TimeoutSeconds = 900" in source
    assert "TimeoutSeconds = 600" in source
    assert "TaskDefinition = aws_ecs_task_definition.worker.arn" in source
    assert "Count          = 1" in source
    assert 'AssignPublicIp = "ENABLED"' in source
    assert "Subnets        = [aws_subnet.task.id]" in source
    assert "SecurityGroups = [aws_security_group.task.id]" in source
    assert "--run-spec-key" in source
    assert "--bucket" not in source
    assert "--table" not in source
    assert "ErrorPath" not in source
    assert "ResultPath = null" in source
    assert "FinalizeFailure" in source
    assert "ClosedAcquireFailure" in source
    assert "ClosedPrepareFailure" in source
    assert "FailureCode = \"WORKER_FAILED\"" in source
    assert "FailureCode = \"WORKFLOW_TIMED_OUT\"" in source
    assert "FailureCode = \"ARTIFACT_VERIFICATION_FAILED\"" in source
    assert "Lambda.ServiceException" in source
    assert "ECS.ServiceException" in source
    assert "BackoffRate = 2" in source
    assert "MaxAttempts = 3" in source
    assert source.count("Lambda.ClientExecutionTimeoutException") == 2
    assert "WorkerTimedOut" in source


def test_runtime_iam_is_separate_and_excludes_wildcard_data_plane_access() -> None:
    source = _source("iam.tf")

    for role in (
        "acquisition_lambda",
        "preparation_lambda",
        "finalization_lambda",
        "results_lambda",
        "ecs_execution",
        "ecs_task",
        "step_functions",
        "scheduler",
    ):
        assert f'aws_iam_role" "{role}"' in source
    assert 'Action   = "s3:*"' not in source
    assert 'Action   = "dynamodb:*"' not in source
    results_policy = source.split('aws_iam_role_policy" "results_lambda"', 1)[1].split(
        'resource "aws_iam_role_policy"', 1
    )[0]
    assert "datasets/v1/*" not in results_policy
    assert "dynamodb:LeadingKeys" in source


def test_step_functions_sync_role_has_the_fixed_eventbridge_wait_permissions() -> None:
    source = _source("iam.tf")

    assert '"events:PutTargets", "events:PutRule", "events:DescribeRule"' in source
    assert "StepFunctionsGetEventsForECSTaskRule" in source
    assert "data.aws_caller_identity.current.account_id" in source
    assert "data.aws_partition.current.partition" in source


def test_runtime_trust_policies_admit_only_the_expected_aws_services() -> None:
    source = _source("iam.tf")

    expected = {
        "lambda_assume_role": "lambda.amazonaws.com",
        "ecs_task_assume_role": "ecs-tasks.amazonaws.com",
        "step_functions_assume_role": "states.amazonaws.com",
        "scheduler_assume_role": "scheduler.amazonaws.com",
    }
    for document, principal in expected.items():
        block = source.split(f'data "aws_iam_policy_document" "{document}"', 1)[1].split(
            '\n}\n', 1
        )[0]
        assert 'actions = ["sts:AssumeRole"]' in block
        assert f'identifiers = ["{principal}"]' in block


def test_public_api_has_only_the_bounded_read_route() -> None:
    source = _source("api_gateway.tf")

    assert 'route_key = "GET /runs/{run_id}"' in source
    assert "POST /runs" not in source
    assert "GET /runs" not in source.replace('GET /runs/{run_id}', "")
    assert "throttling_rate_limit  = 2" in source
    assert "throttling_burst_limit = 5" in source
    assert "cors_configuration" not in source
    assert "results_lambda_permission_source_arn" in source
    assert "source_arn    = local.results_lambda_permission_source_arn" in source


def test_runtime_definitions_pin_image_and_bound_compute() -> None:
    lambda_source = _source("lambda.tf")
    ecs_source = _source("ecs.tf")
    all_infra = "\n".join(path.read_text(encoding="utf-8") for path in INFRA.glob("*.tf"))

    assert lambda_source.count('resource "aws_lambda_function"') == 4
    assert lambda_source.count("local.runtime_image_uri") == 4
    for timeout in (420, 120, 180, 60):
        assert f"timeout     = {timeout}" in lambda_source
    assert "reserved_concurrent_executions = 2" in lambda_source
    assert ecs_source.count('resource "aws_ecs_task_definition"') == 1
    assert 'resource "aws_ecs_service"' not in all_infra
    assert "cpu                      = \"512\"" in ecs_source
    assert "memory                   = \"1024\"" in ecs_source
    assert "readonlyRootFilesystem = true" in ecs_source
    assert "portMappings" not in ecs_source
    assert "src.cloud.worker" in ecs_source


def test_ecs_stopped_task_alarm_uses_filtered_eventbridge_failures() -> None:
    source = _source("cloudwatch.tf")

    assert source.count('resource "aws_cloudwatch_event_rule"') == 2
    assert source.count('resource "aws_cloudwatch_event_target"') == 2
    assert source.count('lastStatus        = ["STOPPED"]') == 2
    assert '"anything-but" = 0' in source
    assert 'stopCode          = ["TaskFailedToStart"]' in source
    assert source.count("taskDefinitionArn = [aws_ecs_task_definition.worker.arn]") == 2
    assert "?ERROR ?error" not in source
    assert "aws_cloudwatch_log_resource_policy" in source
    assert 'Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]' in source


def test_scheduler_retries_are_explicitly_cost_bounded() -> None:
    source = _source("eventbridge.tf")

    assert "retry_policy" in source
    assert "maximum_event_age_in_seconds = 60" in source
    assert "maximum_retry_attempts       = 1" in source
