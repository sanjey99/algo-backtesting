data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name_prefix                  = "${var.project}-${var.environment}"
  runtime_permissions_boundary = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/${local.name_prefix}-runtime-boundary"
  state_object_arn             = "${aws_s3_bucket.state.arn}/${var.backend_state_key}"
  state_lock_arn               = "${local.state_object_arn}.tflock"
  artifact_bucket_arn          = "arn:${data.aws_partition.current.partition}:s3:::${local.name_prefix}-art-*"
  artifact_object_arns         = ["${local.artifact_bucket_arn}/datasets/v1/*", "${local.artifact_bucket_arn}/runs/v1/*"]
  runs_table_arn               = "arn:${data.aws_partition.current.partition}:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${local.name_prefix}-runs"
  worker_repository_arn        = "arn:${data.aws_partition.current.partition}:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/${local.name_prefix}-worker"
  github_budget_arn            = "arn:${data.aws_partition.current.partition}:budgets::${data.aws_caller_identity.current.account_id}:budget/${local.name_prefix}-monthly-guardrail"
  lambda_function_arns         = [for name in ["acquisition", "preparation", "finalization", "results"] : "arn:${data.aws_partition.current.partition}:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-${name}"]
  runtime_log_stream_arns = [
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*:log-stream:*",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${local.name_prefix}-worker:log-stream:*",
  ]
  runtime_log_group_arns = [
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${local.name_prefix}-worker",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/events/${local.name_prefix}-ecs-task-failures",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/vendedlogs/states/${local.name_prefix}-research",
    "arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/apigateway/${local.name_prefix}-public-results",
  ]
  ecs_cluster_arn         = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${local.name_prefix}-research"
  worker_task_arn         = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name_prefix}-worker:*"
  step_functions_rule_arn = "arn:${data.aws_partition.current.partition}:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
  eventbridge_rule_arns = [
    "arn:${data.aws_partition.current.partition}:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/${local.name_prefix}-ecs-worker-nonzero-exit",
    "arn:${data.aws_partition.current.partition}:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/${local.name_prefix}-ecs-worker-failed-to-start",
  ]
  scheduler_arn     = "arn:${data.aws_partition.current.partition}:scheduler:${var.region}:${data.aws_caller_identity.current.account_id}:schedule/default/${local.name_prefix}-research"
  state_machine_arn = "arn:${data.aws_partition.current.partition}:states:${var.region}:${data.aws_caller_identity.current.account_id}:stateMachine:${local.name_prefix}-research"
  cloudwatch_alarm_arns = [
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-workflow-failures",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-workflow-timeouts",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-acquisition-errors",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-preparation-errors",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-finalization-errors",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-results-errors",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-acquisition-throttles",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-preparation-throttles",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-finalization-throttles",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-results-throttles",
    "arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${data.aws_caller_identity.current.account_id}:alarm:${local.name_prefix}-ecs-worker-failures",
  ]
  runtime_role_arns = [for name in ["acquisition-lambda", "preparation-lambda", "finalization-lambda", "results-lambda", "ecs-execution", "ecs-task", "step-functions", "scheduler"] : "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-${name}"]
}

resource "aws_iam_policy" "runtime_permissions_boundary" {
  name        = "${local.name_prefix}-runtime-boundary"
  description = "Maximum permissions for the bounded runtime execution roles."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"], Resource = local.artifact_object_arns },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = local.runs_table_arn },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = local.worker_repository_arn },
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = local.lambda_function_arns },
      { Effect = "Allow", Action = ["ecs:RunTask"], Resource = local.worker_task_arn },
      { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = local.step_functions_rule_arn },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [local.runtime_role_arns[4], local.runtime_role_arns[5]] },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = local.state_machine_arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = local.runtime_log_stream_arns },
      # Unscopable: ECR authorization tokens and ECS task ARNs are not known until RunTask.
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:StopTask"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = local.ecs_cluster_arn } } },
      # Unscopable: Step Functions CloudWatch Logs delivery APIs accept no log-group ARN.
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:DeleteLogDelivery", "logs:DescribeLogGroups", "logs:DescribeResourcePolicies", "logs:GetLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:UpdateLogDelivery"], Resource = "*" },
    ]
  })
}

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
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.deploy_environment}"]
    }
  }
}

resource "aws_iam_role" "github_plan" {
  name               = "${local.name_prefix}-github-plan"
  assume_role_policy = data.aws_iam_policy_document.github_plan_assume_role.json
}

resource "aws_iam_role" "github_deploy" {
  name               = "${local.name_prefix}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_deploy_assume_role.json
}

resource "aws_iam_role_policy" "github_plan" {
  name = "read-only-terraform-plan-and-state-lock"
  role = aws_iam_role.github_plan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn, Condition = { StringEquals = { "s3:prefix" = [var.backend_state_key, "${var.backend_state_key}.tflock"] } } },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.state_object_arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = local.state_lock_arn },
      # Unscopable discovery APIs required by Terraform refresh; this role has no mutating runtime APIs.
      { Effect = "Allow", Action = ["apigateway:GET", "cloudwatch:DescribeAlarms", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:ListTagsOfResource", "ec2:DescribeAvailabilityZones", "ec2:DescribeInternetGateways", "ec2:DescribeRouteTables", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ec2:DescribeTags", "ec2:DescribeVpcAttribute", "ec2:DescribeVpcs", "ecr:DescribeRepositories", "ecs:DescribeClusters", "ecs:DescribeTaskDefinition", "events:DescribeRule", "events:ListTargetsByRule", "lambda:GetFunction", "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConcurrency", "lambda:GetPolicy", "logs:DescribeLogGroups", "logs:DescribeMetricFilters", "logs:DescribeResourcePolicies", "scheduler:GetSchedule", "states:DescribeStateMachine", "tag:GetResources"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      { Effect = "Allow", Action = ["budgets:DescribeBudget"], Resource = local.github_budget_arn },
      { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = local.runtime_permissions_boundary },
      { Effect = "Allow", Action = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:ListRoleTags"], Resource = local.runtime_role_arns },
      { Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
      { Effect = "Allow", Action = ["s3:GetBucketAcl", "s3:GetBucketLocation", "s3:GetBucketOwnershipControls", "s3:GetBucketPolicy", "s3:GetBucketPolicyStatus", "s3:GetBucketPublicAccessBlock", "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration", "s3:ListBucket"], Resource = local.artifact_bucket_arn },
      { Effect = "Allow", Action = ["ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy", "ecr:ListTagsForResource"], Resource = local.worker_repository_arn },
      { Effect = "Allow", Action = ["ecs:ListTagsForResource"], Resource = [local.ecs_cluster_arn, local.worker_task_arn] },
      { Effect = "Allow", Action = ["events:ListTagsForResource"], Resource = local.eventbridge_rule_arns },
      { Effect = "Allow", Action = ["lambda:ListTags"], Resource = local.lambda_function_arns },
      { Effect = "Allow", Action = ["logs:ListTagsForResource"], Resource = local.runtime_log_group_arns },
      { Effect = "Allow", Action = ["scheduler:ListTagsForResource"], Resource = local.scheduler_arn },
      { Effect = "Allow", Action = ["states:ListTagsForResource"], Resource = local.state_machine_arn },
      { Effect = "Allow", Action = ["cloudwatch:ListTagsForResource"], Resource = local.cloudwatch_alarm_arns },
    ]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "bounded-terraform-deployment"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = aws_s3_bucket.state.arn },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn, Condition = { StringEquals = { "s3:prefix" = [var.backend_state_key, "${var.backend_state_key}.tflock"] } } },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = local.state_object_arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = local.state_lock_arn },
      { Effect = "Allow", Action = ["iam:CreateRole"], Resource = local.runtime_role_arns, Condition = { StringEquals = { "iam:PermissionsBoundary" = local.runtime_permissions_boundary } } },
      { Effect = "Allow", Action = ["iam:DeleteRole", "iam:DeleteRolePolicy", "iam:GetRole", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:ListRolePolicies", "iam:ListRoleTags", "iam:PutRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:UpdateAssumeRolePolicy"], Resource = local.runtime_role_arns },
      { Effect = "Allow", Action = ["iam:PutRolePermissionsBoundary"], Resource = local.runtime_role_arns, Condition = { StringEquals = { "iam:PermissionsBoundary" = local.runtime_permissions_boundary } } },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = local.runtime_role_arns, Condition = { StringLike = { "iam:PassedToService" = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com", "states.amazonaws.com", "scheduler.amazonaws.com"] } } },
      { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = local.runtime_permissions_boundary },
      { Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
      { Effect = "Allow", Action = ["budgets:CreateBudget", "budgets:DeleteBudget", "budgets:DescribeBudget", "budgets:ModifyBudget"], Resource = local.github_budget_arn },
      # API Gateway v2 uses generic HTTP IAM actions and supplies no compatible ARN scope.
      { Effect = "Allow", Action = ["apigateway:DELETE", "apigateway:GET", "apigateway:PATCH", "apigateway:POST", "apigateway:PUT"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      # Account-level Logs discovery and resource-policy APIs have no compatible log-group ARN scope.
      { Effect = "Allow", Action = ["logs:DeleteResourcePolicy", "logs:DescribeLogGroups", "logs:DescribeResourcePolicies", "logs:PutResourcePolicy"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:DeleteMetricFilter", "logs:DeleteRetentionPolicy", "logs:DescribeMetricFilters", "logs:ListTagsForResource", "logs:PutMetricFilter", "logs:PutRetentionPolicy", "logs:TagResource", "logs:UntagResource"], Resource = local.runtime_log_group_arns },
      { Effect = "Allow", Action = ["lambda:AddPermission", "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:DeleteFunctionConcurrency", "lambda:GetFunction", "lambda:GetFunctionCodeSigningConfig", "lambda:GetFunctionConcurrency", "lambda:GetPolicy", "lambda:ListTags", "lambda:PutFunctionConcurrency", "lambda:RemovePermission", "lambda:TagResource", "lambda:UntagResource", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"], Resource = local.lambda_function_arns },
      { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:CreateRepository", "ecr:DeleteLifecyclePolicy", "ecr:DeleteRepository", "ecr:DeleteRepositoryPolicy", "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetLifecyclePolicy", "ecr:GetRepositoryPolicy", "ecr:InitiateLayerUpload", "ecr:ListTagsForResource", "ecr:PutImage", "ecr:PutImageScanningConfiguration", "ecr:PutImageTagMutability", "ecr:PutLifecyclePolicy", "ecr:SetRepositoryPolicy", "ecr:TagResource", "ecr:UntagResource", "ecr:UploadLayerPart"], Resource = local.worker_repository_arn },
      # Unscopable: ECR registry authorization tokens are account-wide by AWS design.
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:ListTagsOfResource", "dynamodb:TagResource", "dynamodb:UntagResource", "dynamodb:UpdateContinuousBackups", "dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive"], Resource = local.runs_table_arn },
      # EC2 control-plane APIs use generated IDs, so they cannot be narrowed before creation.
      { Effect = "Allow", Action = ["ec2:AssociateRouteTable", "ec2:AttachInternetGateway", "ec2:AuthorizeSecurityGroupEgress", "ec2:CreateInternetGateway", "ec2:CreateRoute", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateSubnet", "ec2:CreateTags", "ec2:CreateVpc", "ec2:DeleteInternetGateway", "ec2:DeleteRoute", "ec2:DeleteRouteTable", "ec2:DeleteSecurityGroup", "ec2:DeleteSubnet", "ec2:DeleteTags", "ec2:DeleteVpc", "ec2:DescribeAvailabilityZones", "ec2:DescribeInternetGateways", "ec2:DescribeRouteTables", "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ec2:DescribeTags", "ec2:DescribeVpcAttribute", "ec2:DescribeVpcs", "ec2:DetachInternetGateway", "ec2:DisassociateRouteTable", "ec2:ModifySubnetAttribute", "ec2:ModifyVpcAttribute", "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      { Effect = "Allow", Action = ["s3:CreateBucket", "s3:DeleteBucket", "s3:DeleteBucketOwnershipControls", "s3:DeleteBucketPolicy", "s3:DeletePublicAccessBlock", "s3:GetBucketAcl", "s3:GetBucketLocation", "s3:GetBucketOwnershipControls", "s3:GetBucketPolicy", "s3:GetBucketPolicyStatus", "s3:GetBucketPublicAccessBlock", "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration", "s3:ListBucket", "s3:PutBucketOwnershipControls", "s3:PutBucketPolicy", "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration", "s3:PutLifecycleConfiguration"], Resource = local.artifact_bucket_arn },
      { Effect = "Allow", Action = ["ecs:CreateCluster", "ecs:DeleteCluster", "ecs:DescribeClusters", "ecs:ListTagsForResource", "ecs:TagResource", "ecs:UntagResource"], Resource = local.ecs_cluster_arn },
      { Effect = "Allow", Action = ["ecs:DeregisterTaskDefinition", "ecs:DescribeTaskDefinition", "ecs:ListTagsForResource", "ecs:TagResource", "ecs:UntagResource"], Resource = local.worker_task_arn },
      # Unscopable: RegisterTaskDefinition has no task-definition ARN until AWS assigns the revision.
      { Effect = "Allow", Action = ["ecs:RegisterTaskDefinition"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
      { Effect = "Allow", Action = ["events:DeleteRule", "events:DescribeRule", "events:ListTagsForResource", "events:ListTargetsByRule", "events:PutRule", "events:PutTargets", "events:RemoveTargets", "events:TagResource", "events:UntagResource"], Resource = local.eventbridge_rule_arns },
      { Effect = "Allow", Action = ["scheduler:CreateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule", "scheduler:ListTagsForResource", "scheduler:TagResource", "scheduler:UntagResource", "scheduler:UpdateSchedule"], Resource = local.scheduler_arn },
      { Effect = "Allow", Action = ["states:CreateStateMachine", "states:DeleteStateMachine", "states:DescribeStateMachine", "states:ListTagsForResource", "states:StartExecution", "states:TagResource", "states:UntagResource", "states:UpdateStateMachine"], Resource = local.state_machine_arn },
      { Effect = "Allow", Action = ["cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "cloudwatch:PutMetricAlarm", "cloudwatch:TagResource", "cloudwatch:UntagResource"], Resource = local.cloudwatch_alarm_arns },
      { Effect = "Allow", Action = ["tag:GetResources"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } } },
    ]
  })
}
