locals {
  runtime_image_uri = "${aws_ecr_repository.worker.repository_url}@sha256:${var.image_digest}"
}

resource "aws_ecs_cluster" "research" {
  #checkov:skip=CKV_AWS_65:Container Insights is intentionally disabled; fourteen-day application logs and bounded alarms are the approved observability surface.
  name = "${local.name_prefix}-research"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # Fargate's bounded task ephemeral store backs the only writable /tmp mount.
  ephemeral_storage {
    size_in_gib = 21
  }

  container_definitions = jsonencode([
    {
      name                   = "worker"
      image                  = local.runtime_image_uri
      essential              = true
      readonlyRootFilesystem = true
      entryPoint             = ["/opt/venv/bin/python", "-m", "src.cloud.worker"]
      command                = []
      stopTimeout            = 30
      environment = [
        { name = "ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.id },
        { name = "RUN_TABLE", value = aws_dynamodb_table.runs.name },
      ]
      mountPoints = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.ecs_worker.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "worker"
        }
      }
    },
  ])

  volume {
    name = "tmp"
  }
}
