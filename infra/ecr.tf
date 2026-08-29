resource "aws_ecr_repository" "worker" {
  #checkov:skip=CKV_AWS_51:Mutable tags are approved for build convenience; every execution is pinned to the admitted immutable digest.
  #checkov:skip=CKV_AWS_136:Default ECR encryption is the approved boundary; a customer-managed KMS key is intentionally excluded.
  name                 = "${local.name_prefix}-worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-worker"
  })
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after one day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Retain at most three tagged images"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = ["*"]
          countType      = "imageCountMoreThan"
          countNumber    = 3
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}

output "ecr_repository_name" {
  description = "Name of the private worker image repository."
  value       = aws_ecr_repository.worker.name
}

output "ecr_repository_url" {
  description = "URL of the private worker image repository."
  value       = aws_ecr_repository.worker.repository_url
}
