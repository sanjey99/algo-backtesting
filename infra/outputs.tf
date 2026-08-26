output "state_machine_arn" {
  description = "ARN of the bounded Standard research workflow."
  value       = aws_sfn_state_machine.research.arn
}

output "public_results_api_base_url" {
  description = "Read-only public HTTP API base URL; it exposes only GET /runs/{run_id}."
  value       = aws_apigatewayv2_stage.public.invoke_url
}

output "ecs_cluster_name" {
  description = "Name of the one-shot research ECS cluster."
  value       = aws_ecs_cluster.research.name
}

output "ecs_worker_task_definition_arn" {
  description = "Pinned-definition ARN used for one bounded Fargate worker task."
  value       = aws_ecs_task_definition.worker.arn
}

output "runtime_log_group_names" {
  description = "Named fourteen-day log groups for runtime diagnostics."
  value = {
    acquisition    = aws_cloudwatch_log_group.lambda_acquisition.name
    preparation    = aws_cloudwatch_log_group.lambda_preparation.name
    finalization   = aws_cloudwatch_log_group.lambda_finalization.name
    results        = aws_cloudwatch_log_group.lambda_results.name
    worker         = aws_cloudwatch_log_group.ecs_worker.name
    task_failures  = aws_cloudwatch_log_group.ecs_task_failures.name
    step_functions = aws_cloudwatch_log_group.step_functions.name
    api_gateway    = aws_cloudwatch_log_group.api_gateway.name
  }
}

output "budget_name" {
  description = "Monthly budget guardrail name; alerts notify but do not stop spend."
  value       = aws_budgets_budget.research.name
}

output "schedule_enabled" {
  description = "Whether the visible AWS Scheduler automation is explicitly enabled."
  value       = var.enable_schedule
}
