resource "aws_scheduler_schedule" "research" {
  #checkov:skip=CKV_AWS_297:AWS-owned encryption is approved and a customer-managed KMS key is intentionally excluded.
  name                         = "${local.name_prefix}-research"
  schedule_expression          = "cron(0 2 ? * MON-FRI *)"
  schedule_expression_timezone = "Asia/Singapore"
  state                        = var.enable_schedule ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.research.arn
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      schema_version      = "1"
      symbol              = "SPY"
      start               = "2024-01-02"
      end                 = "2024-01-10"
      strategy_key        = "ma_crossover"
      strategy_parameters = { fast_window = 10, slow_window = 20 }
      initial_capital     = 10000.0
      commission_pct      = 0.001
      slippage_pct        = 0.0005
      visibility          = "PRIVATE"
    })
  }
}
