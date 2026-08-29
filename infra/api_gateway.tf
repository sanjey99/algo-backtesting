locals {
  results_lambda_permission_source_arn = "${aws_apigatewayv2_api.public.execution_arn}/*/GET/runs/*"
}

resource "aws_apigatewayv2_api" "public" {
  name          = "${local.name_prefix}-public-results"
  protocol_type = "HTTP"
  description   = "Read-only public results API; it cannot start or list research runs."
}

resource "aws_apigatewayv2_integration" "results" {
  api_id                 = aws_apigatewayv2_api.public.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.results.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "run" {
  #checkov:skip=CKV_AWS_309:This is the deliberately public, read-only GET endpoint; the handler independently proves visibility and finalization.
  api_id    = aws_apigatewayv2_api.public.id
  route_key = "GET /runs/{run_id}"
  target    = "integrations/${aws_apigatewayv2_integration.results.id}"
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  #checkov:skip=CKV_AWS_338:The approved fourteen-day retention bounds demo diagnostic cost.
  #checkov:skip=CKV_AWS_158:AWS-managed log encryption is the approved boundary; customer-managed KMS is excluded.
  name              = "/aws/apigateway/${local.name_prefix}-public-results"
  retention_in_days = 14
}

resource "aws_apigatewayv2_stage" "public" {
  api_id      = aws_apigatewayv2_api.public.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 2
    throttling_burst_limit = 5
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format          = jsonencode({ requestId = "$context.requestId", routeKey = "$context.routeKey", status = "$context.status", responseLength = "$context.responseLength" })
  }
}

resource "aws_lambda_permission" "api_gateway_results" {
  statement_id  = "AllowPublicResultsHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.results.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = local.results_lambda_permission_source_arn
}
