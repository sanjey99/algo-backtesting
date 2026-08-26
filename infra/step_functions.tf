locals {
  # Keep the ASL plan-known so native tests can inspect the exact definition
  # before the provider computes aws_sfn_state_machine.definition.
  research_state_machine_definition = jsonencode({
    Comment        = "Bounded immutable research run"
    StartAt        = "AcquireData"
    TimeoutSeconds = 900
    States = {
      AcquireData = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = aws_lambda_function.acquisition.arn, "Payload.$" = "$" }
        OutputPath = "$.Payload"
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"], IntervalSeconds = 2, BackoffRate = 2, MaxAttempts = 3 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "ClosedAcquireFailure" }]
        Next       = "PrepareRun"
      }
      ClosedAcquireFailure = {
        Type  = "Fail"
        Error = "ACQUISITION_FAILED"
        Cause = "Acquisition failed before a durable run record existed."
      }
      PrepareRun = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = aws_lambda_function.preparation.arn, "Payload.$" = "$" }
        OutputPath = "$.Payload"
        Retry      = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException"], IntervalSeconds = 2, BackoffRate = 2, MaxAttempts = 3 }]
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "ClosedPrepareFailure" }]
        Next       = "RunWorker"
      }
      ClosedPrepareFailure = {
        Type  = "Fail"
        Error = "PREPARATION_FAILED"
        Cause = "Preparation failed before a durable run record was returned."
      }
      RunWorker = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.research.arn
          TaskDefinition = aws_ecs_task_definition.worker.arn
          LaunchType     = "FARGATE"
          Count          = 1
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              AssignPublicIp = "ENABLED"
              Subnets        = [aws_subnet.task.id]
              SecurityGroups = [aws_security_group.task.id]
            }
          }
          Overrides = {
            ContainerOverrides = [{ Name = "worker", "Command.$" = "States.Array('--run-spec-key', $.run_spec_key)" }]
          }
        }
        Retry = [{ ErrorEquals = ["ECS.ServiceException", "ECS.AmazonECSException"], IntervalSeconds = 2, BackoffRate = 2, MaxAttempts = 3 }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "WorkerFailed" }]
        # Preserve only PrepareRun's run_id/key; ECS response metadata is not durable state.
        ResultPath = null
        Next       = "FinalizeSuccess"
      }
      WorkerFailed = {
        Type       = "Pass"
        Parameters = { "run_id.$" = "$.run_id", Outcome = "FAILED", FailureCode = "WORKER_FAILED" }
        Next       = "FinalizeFailure"
      }
      FinalizeSuccess = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = aws_lambda_function.finalization.arn, Payload = { "run_id.$" = "$.run_id", outcome = "SUCCEEDED" } }
        OutputPath = "$.Payload"
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizationFailed" }]
        End        = true
      }
      FinalizationFailed = {
        Type       = "Pass"
        Parameters = { "run_id.$" = "$.run_id", Outcome = "FAILED", FailureCode = "ARTIFACT_VERIFICATION_FAILED" }
        Next       = "FinalizeFailure"
      }
      FinalizeFailure = {
        Type       = "Task"
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = aws_lambda_function.finalization.arn, Payload = { "run_id.$" = "$.run_id", outcome = "FAILED", "failure_code.$" = "$.FailureCode" } }
        OutputPath = "$.Payload"
        End        = true
      }
    }
  })
}

resource "aws_sfn_state_machine" "research" {
  #checkov:skip=CKV_AWS_284:X-Ray is intentionally excluded from this short-lived cost-bounded demonstration.
  #checkov:skip=CKV_AWS_285:ERROR-only execution logs with execution data excluded avoid persisting raw workflow inputs while retaining failure diagnostics.
  name       = "${local.name_prefix}-research"
  role_arn   = aws_iam_role.step_functions.arn
  type       = "STANDARD"
  definition = local.research_state_machine_definition

  logging_configuration {
    include_execution_data = false
    level                  = "ERROR"
    log_destination        = "${aws_cloudwatch_log_group.step_functions.arn}:*"
  }

  tracing_configuration {
    enabled = false
  }
}
