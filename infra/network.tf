resource "aws_vpc" "research" {
  #checkov:skip=CKV2_AWS_11:VPC flow logs require persistent logging and IAM resources that belong to the later runtime-observability task.
  cidr_block                       = "10.42.0.0/16"
  enable_dns_support               = true
  enable_dns_hostnames             = true
  assign_generated_ipv6_cidr_block = false

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-vpc"
  })
}

resource "aws_default_security_group" "research" {
  vpc_id = aws_vpc.research.id

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-default-deny"
  })
}

resource "aws_subnet" "task" {
  vpc_id                          = aws_vpc.research.id
  cidr_block                      = "10.42.1.0/24"
  map_public_ip_on_launch         = false
  assign_ipv6_address_on_creation = false

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-task"
  })
}

resource "aws_internet_gateway" "research" {
  vpc_id = aws_vpc.research.id

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-igw"
  })
}

resource "aws_route_table" "task" {
  vpc_id = aws_vpc.research.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.research.id
  }

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-task"
  })
}

resource "aws_route_table_association" "task" {
  subnet_id      = aws_subnet.task.id
  route_table_id = aws_route_table.task.id
}

resource "aws_security_group" "task" {
  #checkov:skip=CKV2_AWS_5:Task 10 attaches this foundation security group to the one-shot ECS task definition.
  name        = "${local.name_prefix}-task"
  description = "No ingress; HTTPS-only IPv4 egress for one-shot research tasks"
  vpc_id      = aws_vpc.research.id

  egress {
    description      = "HTTPS to AWS and approved market-data endpoints"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = []
  }

  tags = merge(local.default_tags, {
    Name = "${local.name_prefix}-task"
  })
}

output "vpc_id" {
  description = "ID of the bounded research VPC."
  value       = aws_vpc.research.id
}

output "task_subnet_id" {
  description = "ID of the subnet used by one-shot tasks."
  value       = aws_subnet.task.id
}

output "task_security_group_id" {
  description = "ID of the no-ingress, HTTPS-egress task security group."
  value       = aws_security_group.task.id
}
