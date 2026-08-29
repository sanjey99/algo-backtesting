terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Supply bucket, key, and region through explicit init arguments only after
  # the separately approved bootstrap stack has been applied.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
