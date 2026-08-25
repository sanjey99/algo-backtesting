mock_provider "aws" {
  override_during = plan

  mock_resource "aws_security_group" {
    defaults = {
      ingress = []
    }
  }

  mock_resource "aws_default_security_group" {
    defaults = {
      ingress = []
      egress  = []
    }
  }
}

variables {
  project            = "algo-backtest"
  environment        = "demo"
  owner              = "sanjeyan"
  cost_center        = "student-aws-credits"
  expiry_date        = "2026-10-04"
  alert_emails       = ["owner@example.com"]
  image_digest       = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  github_repository  = "sanjeyan/algo-backtesting"
  deploy_ref         = "refs/heads/main"
  deploy_environment = "aws-demo"
}

run "foundation_uses_cost_safe_defaults" {
  command = plan

  assert {
    condition     = var.region == "ap-southeast-1"
    error_message = "The default AWS region must remain ap-southeast-1."
  }

  assert {
    condition     = var.enable_schedule == false
    error_message = "Paid scheduled execution must be disabled by default."
  }

  assert {
    condition     = aws_s3_bucket.artifacts.force_destroy == false
    error_message = "The artifact bucket must not delete retained versions implicitly."
  }

  assert {
    condition     = aws_s3_bucket_ownership_controls.artifacts.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "The artifact bucket must enforce bucket-owner ownership."
  }

  assert {
    condition     = aws_s3_bucket_versioning.artifacts.versioning_configuration[0].status == "Enabled"
    error_message = "The artifact bucket must have versioning enabled."
  }

  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.artifacts.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "The artifact bucket must use approved SSE-S3 encryption."
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.artifacts.block_public_acls,
      aws_s3_bucket_public_access_block.artifacts.block_public_policy,
      aws_s3_bucket_public_access_block.artifacts.ignore_public_acls,
      aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets,
    ])
    error_message = "Every S3 public-access block must remain enabled."
  }

  assert {
    condition = {
      for rule in aws_s3_bucket_lifecycle_configuration.artifacts.rule : rule.id => rule
    }["abort-incomplete-multipart"].abort_incomplete_multipart_upload[0].days_after_initiation == 1
    error_message = "Incomplete multipart uploads must be aborted after one day."
  }

  assert {
    condition = {
      for rule in aws_s3_bucket_lifecycle_configuration.artifacts.rule : rule.id => rule
    }["expire-transient"].expiration[0].days == 45
    error_message = "Objects tagged LifecycleClass=transient must expire after 45 days."
  }

  assert {
    condition = {
      for rule in aws_s3_bucket_lifecycle_configuration.artifacts.rule : rule.id => rule
    }["expire-selected-public"].expiration[0].days == 90
    error_message = "Objects tagged LifecycleClass=selected-public must expire after 90 days."
  }

  assert {
    condition = {
      for rule in aws_s3_bucket_lifecycle_configuration.artifacts.rule : rule.id => rule
      }["expire-transient"].filter[0].tag[0] == {
      key   = "LifecycleClass"
      value = "transient"
    }
    error_message = "The 45-day lifecycle must target only the transient retention class."
  }

  assert {
    condition = {
      for rule in aws_s3_bucket_lifecycle_configuration.artifacts.rule : rule.id => rule
      }["expire-selected-public"].filter[0].tag[0] == {
      key   = "LifecycleClass"
      value = "selected-public"
    }
    error_message = "The 90-day lifecycle must target only the selected-public retention class."
  }

  assert {
    condition     = aws_dynamodb_table.runs.billing_mode == "PAY_PER_REQUEST"
    error_message = "The run table must use on-demand billing."
  }

  assert {
    condition     = aws_dynamodb_table.runs.ttl[0].enabled && aws_dynamodb_table.runs.ttl[0].attribute_name == "expires_at"
    error_message = "The run table must expire records through expires_at TTL."
  }

  assert {
    condition     = aws_dynamodb_table.runs.server_side_encryption[0].enabled
    error_message = "The run table must have server-side encryption enabled."
  }

  assert {
    condition     = aws_dynamodb_table.runs.point_in_time_recovery[0].enabled == false
    error_message = "PITR must remain disabled for the bounded demonstration."
  }

  assert {
    condition     = aws_dynamodb_table.runs.deletion_protection_enabled == false
    error_message = "Deletion protection must remain disabled so the demo can be cleaned up."
  }

  assert {
    condition     = aws_ecr_repository.worker.image_tag_mutability == "MUTABLE"
    error_message = "Build tags may remain mutable; execution is pinned by image digest."
  }

  assert {
    condition     = aws_ecr_repository.worker.image_scanning_configuration[0].scan_on_push
    error_message = "The private worker repository must scan images on push."
  }

  assert {
    condition = {
      for rule in jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules : rule.rulePriority => rule
    }[2].selection.countNumber == 3
    error_message = "The repository must retain no more than three tagged images."
  }

  assert {
    condition = {
      for rule in jsondecode(aws_ecr_lifecycle_policy.worker.policy).rules : rule.rulePriority => rule
    }[1].selection.countNumber == 1
    error_message = "Untagged images must expire after one day."
  }

  assert {
    condition     = aws_vpc.research.assign_generated_ipv6_cidr_block == false
    error_message = "The bounded network must not allocate IPv6 space."
  }

  assert {
    condition     = aws_subnet.task.map_public_ip_on_launch == false && aws_subnet.task.assign_ipv6_address_on_creation == false
    error_message = "The task subnet must not auto-assign public IPv4 addresses by default and must not assign IPv6 on creation."
  }

  assert {
    condition     = length(aws_security_group.task.ingress) == 0
    error_message = "The task security group must have no ingress rules."
  }

  assert {
    condition = length(aws_security_group.task.egress) == 1 && alltrue([
      for rule in aws_security_group.task.egress :
      rule.protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443 && length(rule.cidr_blocks) == 1 && contains(rule.cidr_blocks, "0.0.0.0/0") && length(rule.ipv6_cidr_blocks) == 0
    ])
    error_message = "The task security group must allow only IPv4 HTTPS egress."
  }

  assert {
    condition     = length(aws_default_security_group.research.ingress) == 0 && length(aws_default_security_group.research.egress) == 0
    error_message = "The VPC default security group must deny all traffic."
  }
}

run "schedule_opt_in_is_explicit" {
  command = plan

  variables {
    enable_schedule = true
  }

  assert {
    condition     = var.enable_schedule
    error_message = "Schedule opt-in must preserve an explicit true value."
  }
}

run "rejects_nonexistent_expiry_date" {
  command = plan

  variables {
    expiry_date = "2026-02-30"
  }

  expect_failures = [var.expiry_date]
}

run "rejects_invalid_alert_email" {
  command = plan

  variables {
    alert_emails = ["not-an-email"]
  }

  expect_failures = [var.alert_emails]
}

run "rejects_invalid_repository" {
  command = plan

  variables {
    github_repository = "missing-owner-separator"
  }

  expect_failures = [var.github_repository]
}

run "rejects_prefixed_or_uppercase_digest" {
  command = plan

  variables {
    image_digest = "sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }

  expect_failures = [var.image_digest]
}
