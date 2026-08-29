resource "aws_s3_bucket" "artifacts" {
  #checkov:skip=CKV_AWS_18:Access logging would require a second persistent bucket; bounded CloudWatch observability is added with the runtime stack.
  #checkov:skip=CKV_AWS_144:Cross-region replication is intentionally excluded from this short-lived, cost-bounded demonstration.
  #checkov:skip=CKV_AWS_145:SSE-S3 is the explicitly approved encryption boundary; a customer-managed KMS key is intentionally excluded.
  #checkov:skip=CKV2_AWS_62:Object event notifications are not part of the approved batch-workflow interface.
  bucket_prefix = "${local.name_prefix}-art-"
  force_destroy = false

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-artifacts"
  })
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*",
      ]
      Condition = {
        Bool = {
          "aws:SecureTransport" = "false"
        }
      }
      },
      {
        Sid       = "DenyPutObjectWithoutLifecycleClass"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource = [
          aws_s3_bucket.artifacts.arn,
          "${aws_s3_bucket.artifacts.arn}/*",
        ]
        Condition = {
          Null = {
            "s3:RequestObjectTag/LifecycleClass" = "true"
          }
        }
      },
      {
        Sid       = "DenyPutObjectWithUnknownLifecycleClass"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource = [
          aws_s3_bucket.artifacts.arn,
          "${aws_s3_bucket.artifacts.arn}/*",
        ]
        Condition = {
          "Null" : {
            "s3:RequestObjectTag/LifecycleClass" = "false"
          },
          StringNotEquals = {
            "s3:RequestObjectTag/LifecycleClass" = ["transient", "selected-public"]
          }
        }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "expire-transient"
    status = "Enabled"

    filter {
      tag {
        key   = local.transient_lifecycle_tag.key
        value = local.transient_lifecycle_tag.value
      }
    }

    expiration {
      days = 45
    }

    noncurrent_version_expiration {
      noncurrent_days = 45
    }
  }

  rule {
    id     = "expire-selected-public"
    status = "Enabled"

    filter {
      tag {
        key   = local.selected_public_lifecycle_tag.key
        value = local.selected_public_lifecycle_tag.value
      }
    }

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

output "artifact_bucket_name" {
  description = "Name of the private artifact bucket."
  value       = aws_s3_bucket.artifacts.bucket
}
