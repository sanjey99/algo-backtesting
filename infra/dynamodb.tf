resource "aws_dynamodb_table" "runs" {
  #checkov:skip=CKV_AWS_28:Point-in-time recovery is explicitly disabled for this bounded, short-lived demonstration.
  #checkov:skip=CKV_AWS_119:AWS-owned DynamoDB encryption is the approved boundary; a customer-managed KMS key is intentionally excluded.
  name                        = "${local.name_prefix}-runs"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "PK"
  deletion_protection_enabled = false

  attribute {
    name = "PK"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-runs"
  })
}

output "run_table_name" {
  description = "Name of the DynamoDB run-state table."
  value       = aws_dynamodb_table.runs.name
}
