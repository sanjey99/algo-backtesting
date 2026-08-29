resource "aws_cloudwatch_log_group" "lambda_acquisition" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/lambda/${local.name_prefix}-acquisition"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_preparation" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/lambda/${local.name_prefix}-preparation"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_finalization" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/lambda/${local.name_prefix}-finalization"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "lambda_results" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/lambda/${local.name_prefix}-results"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "ecs_worker" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/ecs/${local.name_prefix}-worker"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "ecs_task_failures" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/events/${local.name_prefix}-ecs-task-failures"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "step_functions" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/vendedlogs/states/${local.name_prefix}-research"
  retention_in_days = 14
}

locals {
  eventbridge_ecs_task_failures_log_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.ecs_task_failures.arn}:*"
    }]
  })
}

resource "aws_cloudwatch_log_resource_policy" "eventbridge_ecs_task_failures" {
  policy_name     = "${local.name_prefix}-eventbridge-ecs-task-failures"
  policy_document = local.eventbridge_ecs_task_failures_log_policy
}

resource "aws_cloudwatch_event_rule" "ecs_worker_nonzero_exit" {
  name        = "${local.name_prefix}-ecs-worker-nonzero-exit"
  description = "Captures nonzero stopped worker tasks without polling."
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn        = [aws_ecs_cluster.research.arn]
      taskDefinitionArn = [aws_ecs_task_definition.worker.arn]
      lastStatus        = ["STOPPED"]
      containers = {
        exitCode = [{ "anything-but" = 0 }]
      }
    }
  })
}

resource "aws_cloudwatch_event_rule" "ecs_worker_failed_to_start" {
  name        = "${local.name_prefix}-ecs-worker-failed-to-start"
  description = "Captures worker startup failures without polling."
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn        = [aws_ecs_cluster.research.arn]
      taskDefinitionArn = [aws_ecs_task_definition.worker.arn]
      lastStatus        = ["STOPPED"]
      stopCode          = ["TaskFailedToStart"]
    }
  })
}

resource "aws_cloudwatch_event_target" "ecs_worker_nonzero_exit" {
  rule      = aws_cloudwatch_event_rule.ecs_worker_nonzero_exit.name
  target_id = "ecs-worker-failure-log"
  arn       = aws_cloudwatch_log_group.ecs_task_failures.arn

  depends_on = [aws_cloudwatch_log_resource_policy.eventbridge_ecs_task_failures]
}

resource "aws_cloudwatch_event_target" "ecs_worker_failed_to_start" {
  rule      = aws_cloudwatch_event_rule.ecs_worker_failed_to_start.name
  target_id = "ecs-worker-start-failure-log"
  arn       = aws_cloudwatch_log_group.ecs_task_failures.arn

  depends_on = [aws_cloudwatch_log_resource_policy.eventbridge_ecs_task_failures]
}

resource "aws_cloudwatch_log_metric_filter" "ecs_worker_failures" {
  name           = "${local.name_prefix}-ecs-worker-failures"
  log_group_name = aws_cloudwatch_log_group.ecs_task_failures.name
  pattern        = ""

  metric_transformation {
    name          = "EcsWorkerFailures"
    namespace     = "${local.name_prefix}/Research"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "step_functions_failures" {
  alarm_name          = "${local.name_prefix}-workflow-failures"
  alarm_description   = "Investigate bounded research workflow failures."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { StateMachineArn = aws_sfn_state_machine.research.arn }
}

resource "aws_cloudwatch_metric_alarm" "step_functions_timeouts" {
  alarm_name          = "${local.name_prefix}-workflow-timeouts"
  alarm_description   = "Investigate research workflows that exceed their bounded execution window."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsTimedOut"
  namespace           = "AWS/States"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { StateMachineArn = aws_sfn_state_machine.research.arn }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = { acquisition = aws_lambda_function.acquisition, preparation = aws_lambda_function.preparation, finalization = aws_lambda_function.finalization, results = aws_lambda_function.results }
  alarm_name          = "${local.name_prefix}-${each.key}-errors"
  alarm_description   = "Investigate Lambda errors for the bounded research workflow."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { FunctionName = each.value.function_name }
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each            = { acquisition = aws_lambda_function.acquisition, preparation = aws_lambda_function.preparation, finalization = aws_lambda_function.finalization, results = aws_lambda_function.results }
  alarm_name          = "${local.name_prefix}-${each.key}-throttles"
  alarm_description   = "Investigate Lambda throttles for the bounded research workflow."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions          = { FunctionName = each.value.function_name }
}

resource "aws_cloudwatch_metric_alarm" "ecs_worker_failures" {
  alarm_name          = "${local.name_prefix}-ecs-worker-failures"
  alarm_description   = "Investigate one-shot ECS worker stopped-task failures without polling."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.ecs_worker_failures.metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.ecs_worker_failures.metric_transformation[0].namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 1
}
