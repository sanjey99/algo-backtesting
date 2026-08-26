resource "aws_lambda_function" "acquisition" {
  #checkov:skip=CKV_AWS_50:X-Ray is intentionally excluded from this short-lived cost-bounded demonstration.
  #checkov:skip=CKV_AWS_117:Putting internet-facing acquisition in the VPC would require excluded NAT or endpoint infrastructure.
  #checkov:skip=CKV_AWS_116:Step Functions owns bounded retries and closed failure routing; no asynchronous Lambda source needs a DLQ.
  #checkov:skip=CKV_AWS_173:Environment values are non-secret resource identifiers; a CMK is intentionally excluded.
  #checkov:skip=CKV_AWS_272:Immutable ECR digests are the approved image-integrity boundary for this demonstration.
  #checkov:skip=CKV_AWS_115:Only the trusted Step Functions role can invoke this internal function; workflow retries bound execution.
  function_name = "${local.name_prefix}-acquisition"
  role          = aws_iam_role.acquisition_lambda.arn
  package_type  = "Image"
  image_uri     = local.runtime_image_uri
  image_config {
    command = ["src.cloud.ingestion_handler.lambda_handler"]
  }
  timeout     = 420
  memory_size = 2048
  ephemeral_storage {
    size = 10240
  }
  environment {
    variables = {
      ARTIFACT_BUCKET = aws_s3_bucket.artifacts.id
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda_acquisition]
}

resource "aws_lambda_function" "preparation" {
  #checkov:skip=CKV_AWS_50:X-Ray is intentionally excluded from this short-lived cost-bounded demonstration.
  #checkov:skip=CKV_AWS_117:Putting internet-facing acquisition in the VPC would require excluded NAT or endpoint infrastructure.
  #checkov:skip=CKV_AWS_116:Step Functions owns bounded retries and closed failure routing; no asynchronous Lambda source needs a DLQ.
  #checkov:skip=CKV_AWS_173:Environment values are non-secret resource identifiers; a CMK is intentionally excluded.
  #checkov:skip=CKV_AWS_272:Immutable ECR digests are the approved image-integrity boundary for this demonstration.
  #checkov:skip=CKV_AWS_115:Only the trusted Step Functions role can invoke this internal function; workflow retries bound execution.
  function_name = "${local.name_prefix}-preparation"
  role          = aws_iam_role.preparation_lambda.arn
  package_type  = "Image"
  image_uri     = local.runtime_image_uri
  image_config {
    command = ["src.cloud.prepare_handler.lambda_handler"]
  }
  timeout     = 120
  memory_size = 1024
  environment {
    variables = {
      ARTIFACT_BUCKET     = aws_s3_bucket.artifacts.id
      RUN_TABLE           = aws_dynamodb_table.runs.name
      ENGINE_IMAGE_DIGEST = "sha256:${var.image_digest}"
      RUN_TTL_DAYS        = "45"
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda_preparation]
}

resource "aws_lambda_function" "finalization" {
  #checkov:skip=CKV_AWS_50:X-Ray is intentionally excluded from this short-lived cost-bounded demonstration.
  #checkov:skip=CKV_AWS_117:Putting internet-facing acquisition in the VPC would require excluded NAT or endpoint infrastructure.
  #checkov:skip=CKV_AWS_116:Step Functions owns bounded retries and closed failure routing; no asynchronous Lambda source needs a DLQ.
  #checkov:skip=CKV_AWS_173:Environment values are non-secret resource identifiers; a CMK is intentionally excluded.
  #checkov:skip=CKV_AWS_272:Immutable ECR digests are the approved image-integrity boundary for this demonstration.
  #checkov:skip=CKV_AWS_115:Only the trusted Step Functions role can invoke this internal function; workflow retries bound execution.
  function_name = "${local.name_prefix}-finalization"
  role          = aws_iam_role.finalization_lambda.arn
  package_type  = "Image"
  image_uri     = local.runtime_image_uri
  image_config {
    command = ["src.cloud.finalize_handler.lambda_handler"]
  }
  timeout     = 180
  memory_size = 1024
  environment {
    variables = {
      ARTIFACT_BUCKET = aws_s3_bucket.artifacts.id
      RUN_TABLE       = aws_dynamodb_table.runs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda_finalization]
}

resource "aws_lambda_function" "results" {
  #checkov:skip=CKV_AWS_50:X-Ray is intentionally excluded from this short-lived cost-bounded demonstration.
  #checkov:skip=CKV_AWS_117:Putting this public Lambda in the VPC would require excluded NAT or endpoint infrastructure.
  #checkov:skip=CKV_AWS_116:HTTP API receives synchronous responses, so no asynchronous Lambda source needs a DLQ.
  #checkov:skip=CKV_AWS_173:Environment values are non-secret resource identifiers; a CMK is intentionally excluded.
  #checkov:skip=CKV_AWS_272:Immutable ECR digests are the approved image-integrity boundary for this demonstration.
  function_name                  = "${local.name_prefix}-results"
  role                           = aws_iam_role.results_lambda.arn
  package_type                   = "Image"
  image_uri                      = local.runtime_image_uri
  reserved_concurrent_executions = 2
  image_config {
    command = ["src.cloud.results_handler.lambda_handler"]
  }
  timeout     = 60
  memory_size = 768
  environment {
    variables = {
      ARTIFACT_BUCKET = aws_s3_bucket.artifacts.id
      RUN_TABLE       = aws_dynamodb_table.runs.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda_results]
}
