data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ecs_task_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "step_functions_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

resource "aws_iam_role" "acquisition_lambda" {
  name               = "${local.name_prefix}-acquisition-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role" "preparation_lambda" {
  name               = "${local.name_prefix}-preparation-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role" "finalization_lambda" {
  name               = "${local.name_prefix}-finalization-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role" "results_lambda" {
  name               = "${local.name_prefix}-results-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role.json
}

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume_role.json
}

resource "aws_iam_role" "step_functions" {
  name               = "${local.name_prefix}-step-functions"
  assume_role_policy = data.aws_iam_policy_document.step_functions_assume_role.json
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

locals {
  lambda_log_actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
  lambda_log_resources = {
    acquisition  = "${aws_cloudwatch_log_group.lambda_acquisition.arn}:*"
    preparation  = "${aws_cloudwatch_log_group.lambda_preparation.arn}:*"
    finalization = "${aws_cloudwatch_log_group.lambda_finalization.arn}:*"
    results      = "${aws_cloudwatch_log_group.lambda_results.arn}:*"
  }
}

resource "aws_iam_role_policy" "acquisition_lambda" {
  name = "artifact-publication"
  role = aws_iam_role.acquisition_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.lambda_log_actions, Resource = local.lambda_log_resources.acquisition },
      {
        Effect = "Allow"
        # Immutable S3 writes attach the lifecycle tag and may verify an existing object.
        Action   = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"]
        Resource = "${aws_s3_bucket.artifacts.arn}/datasets/v1/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "preparation_lambda" {
  name = "prepare-immutable-run"
  role = aws_iam_role.preparation_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.lambda_log_actions, Resource = local.lambda_log_resources.preparation },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/datasets/v1/*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"], Resource = "${aws_s3_bucket.artifacts.arn}/runs/v1/*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.runs.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["RUN#*"] }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "finalization_lambda" {
  name = "finalize-immutable-run"
  role = aws_iam_role.finalization_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.lambda_log_actions, Resource = local.lambda_log_resources.finalization },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/runs/v1/*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.runs.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["RUN#*"] }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "results_lambda" {
  name = "read-finalized-public-results"
  role = aws_iam_role.results_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = local.lambda_log_actions, Resource = local.lambda_log_resources.results },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/runs/v1/*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.runs.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["RUN#*"] }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_execution" {
  name = "pull-image-and-write-worker-logs"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.worker.arn
      },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ecs_worker.arn}:*" },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task" {
  name = "execute-one-bounded-run"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.artifacts.arn}/datasets/v1/*", "${aws_s3_bucket.artifacts.arn}/runs/v1/*"] },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:PutObjectTagging"], Resource = "${aws_s3_bucket.artifacts.arn}/runs/v1/*" },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.runs.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["RUN#*"] }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "step_functions" {
  name = "orchestrate-fixed-runtime"
  role = aws_iam_role.step_functions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [aws_lambda_function.acquisition.arn, aws_lambda_function.preparation.arn, aws_lambda_function.finalization.arn] },
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = aws_ecs_task_definition.worker.arn },
      {
        Effect   = "Allow"
        Action   = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource = "*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.research.arn }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = "arn:${data.aws_partition.current.partition}:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
      },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn] },
      # AWS's Step Functions log-delivery control plane has no resource ARN scope.
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "start-fixed-research-workflow"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["states:StartExecution"], Resource = aws_sfn_state_machine.research.arn }]
  })
}
