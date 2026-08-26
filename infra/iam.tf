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

resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_plan_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:${var.deploy_ref}"]
    }
  }
}

data "aws_iam_policy_document" "github_deploy_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # GitHub emits this protected-environment subject instead of a ref subject.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.deploy_environment}"]
    }
  }
}

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

resource "aws_iam_role" "github_plan" {
  name               = "${local.name_prefix}-github-plan"
  assume_role_policy = data.aws_iam_policy_document.github_plan_assume_role.json
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name_prefix}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume_role.json
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

# GitHub OIDC roles must be target-bootstrapable before any runtime resource
# exists. These patterns intentionally use the immutable project/environment
# naming contract rather than references that would add runtime graph edges.
locals {
  github_state_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.project}-${var.environment}-tfstate-*"
  github_state_object_arn = "${local.github_state_bucket_arn}/${var.backend_state_key}"
  github_state_lock_arn   = "${local.github_state_object_arn}.tflock"

  github_artifact_bucket_arn    = "arn:${data.aws_partition.current.partition}:s3:::${local.name_prefix}-art-*"
  github_runs_table_arn         = "arn:${data.aws_partition.current.partition}:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${local.name_prefix}-runs"
  github_worker_repository_arn  = "arn:${data.aws_partition.current.partition}:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${local.name_prefix}-worker"
  github_lambda_arn_pattern     = "arn:${data.aws_partition.current.partition}:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-*"
  github_ecs_cluster_arn        = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${local.name_prefix}-research"
  github_event_rule_arn_pattern = "arn:${data.aws_partition.current.partition}:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/${local.name_prefix}-ecs-worker-*"
  github_scheduler_arn          = "arn:${data.aws_partition.current.partition}:scheduler:${var.region}:${data.aws_caller_identity.current.account_id}:schedule/default/${local.name_prefix}-research"
  github_state_machine_arn      = "arn:${data.aws_partition.current.partition}:states:${var.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.name_prefix}-research"
  github_alarm_arn_pattern      = "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-*"
  github_log_group_arn_patterns = [
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${local.name_prefix}-worker",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/events/${local.name_prefix}-ecs-task-failures",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/vendedlogs/states/${local.name_prefix}-research",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/apigateway/${local.name_prefix}-public-results",
  ]
  github_budget_arn = "arn:${data.aws_partition.current.partition}:budgets::${data.aws_caller_identity.current.account_id}:budget/${local.name_prefix}-monthly-guardrail"
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

resource "aws_iam_role_policy" "github_plan" {
  name = "read-only-terraform-plan-and-state-lock"
  role = aws_iam_role.github_plan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = local.github_state_bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.github_state_bucket_arn
        Condition = {
          StringEquals = {
            "s3:prefix" = [var.backend_state_key, "${var.backend_state_key}.tflock"]
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = local.github_state_object_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.github_state_lock_arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviderTags"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:ListRoleTags"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["budgets:Describe*"]
        Resource = local.github_budget_arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:ListOpenIDConnectProviders", "iam:ListRoles", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "apigateway:GET", "cloudwatch:Describe*", "dynamodb:Describe*", "dynamodb:List*", "ec2:Describe*",
          "ecr:Describe*", "ecs:Describe*", "ecs:List*", "events:Describe*", "events:List*",
          "lambda:Get*", "lambda:List*", "logs:Describe*", "logs:List*", "scheduler:Get*", "scheduler:List*",
          "states:Describe*", "states:List*", "tag:GetResources"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.region
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "bounded-terraform-deployment"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = local.github_state_bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = local.github_state_bucket_arn
        Condition = {
          StringEquals = {
            "s3:prefix" = [var.backend_state_key, "${var.backend_state_key}.tflock"]
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = local.github_state_object_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = local.github_state_lock_arn
      },
      {
        Effect   = "Allow"
        Action   = ["budgets:CreateBudget", "budgets:ModifyBudget", "budgets:DeleteBudget", "budgets:Describe*"]
        Resource = local.github_budget_arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:ListOpenIDConnectProviders", "iam:ListRoles", "sts:GetCallerIdentity"]
        Resource = "*"
      },
      # These Terraform provider reads and APIs do not support resource ARNs. They are
      # limited to the configured region and are deliberately separate from mutations.
      {
        Effect = "Allow"
        Action = [
          "apigateway:GET", "cloudwatch:Describe*", "dynamodb:Describe*", "dynamodb:List*", "ec2:Describe*",
          "ecr:Describe*", "ecr:GetAuthorizationToken", "ecs:Describe*", "ecs:List*", "events:Describe*", "events:List*",
          "lambda:Get*", "lambda:List*", "logs:Describe*", "logs:List*", "scheduler:Get*", "scheduler:List*",
          "states:Describe*", "states:List*", "tag:GetResources"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.region
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider", "iam:GetOpenIDConnectProvider", "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider", "iam:UpdateOpenIDConnectProviderThumbprint"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
        Condition = {
          StringLike = {
            "iam:PassedToService" = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com", "states.amazonaws.com", "scheduler.amazonaws.com"]
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucketAcl", "s3:GetBucketLocation", "s3:ListBucket",
          "s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls", "s3:GetBucketPublicAccessBlock",
          "s3:PutBucketPublicAccessBlock", "s3:GetBucketVersioning", "s3:PutBucketVersioning",
          "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration", "s3:GetBucketPolicy", "s3:PutBucketPolicy",
          "s3:DeleteBucketPolicy", "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration", "s3:GetBucketTagging",
          "s3:PutBucketTagging"
        ]
        Resource = local.github_artifact_bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:Describe*", "dynamodb:ListTagsOfResource", "dynamodb:TagResource", "dynamodb:UntagResource", "dynamodb:UpdateContinuousBackups", "dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive"]
        Resource = local.github_runs_table_arn
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:CreateRepository", "ecr:DeleteLifecyclePolicy", "ecr:DeleteRepository", "ecr:DeleteRepositoryPolicy", "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:PutLifecyclePolicy", "ecr:SetRepositoryPolicy", "ecr:TagResource", "ecr:UntagResource", "ecr:UploadLayerPart"]
        Resource = local.github_worker_repository_arn
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:AddPermission", "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:DeleteFunctionConcurrency", "lambda:Get*", "lambda:PutFunctionConcurrency", "lambda:RemovePermission", "lambda:TagResource", "lambda:UntagResource", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"]
        Resource = local.github_lambda_arn_pattern
      },
      {
        Effect   = "Allow"
        Action   = ["ecs:DeleteCluster", "ecs:DescribeClusters", "ecs:TagResource", "ecs:UntagResource"]
        Resource = local.github_ecs_cluster_arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:CreateCluster", "ecs:DeregisterTaskDefinition", "ecs:RegisterTaskDefinition",
          "logs:PutResourcePolicy", "logs:DeleteResourcePolicy", "logs:DescribeResourcePolicies",
          "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.region
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["events:DeleteRule", "events:PutRule", "events:PutTargets", "events:RemoveTargets", "events:TagResource", "events:UntagResource"]
        Resource = local.github_event_rule_arn_pattern
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"]
        Resource = local.github_alarm_arn_pattern
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:DescribeLogGroups", "logs:PutRetentionPolicy", "logs:PutMetricFilter", "logs:DeleteMetricFilter", "logs:TagResource", "logs:UntagResource"]
        Resource = local.github_log_group_arn_patterns
      },
      {
        Effect   = "Allow"
        Action   = ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule", "scheduler:TagResource", "scheduler:UntagResource", "scheduler:UpdateSchedule"]
        Resource = local.github_scheduler_arn
      },
      {
        Effect   = "Allow"
        Action   = ["states:CreateStateMachine", "states:DeleteStateMachine", "states:DescribeStateMachine", "states:TagResource", "states:UntagResource", "states:UpdateStateMachine"]
        Resource = local.github_state_machine_arn
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:AssociateRouteTable", "ec2:AttachInternetGateway", "ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress", "ec2:CreateInternetGateway", "ec2:CreateRoute", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateSubnet", "ec2:CreateTags", "ec2:CreateVpc", "ec2:DeleteInternetGateway", "ec2:DeleteRouteTable", "ec2:DeleteSecurityGroup", "ec2:DeleteSubnet", "ec2:DeleteTags", "ec2:DeleteVpc", "ec2:DetachInternetGateway", "ec2:DisassociateRouteTable", "ec2:ModifyVpcAttribute", "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.region
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = local.github_state_machine_arn
      },
    ]
  })
}
