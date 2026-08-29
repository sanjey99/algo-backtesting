provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      Owner       = var.owner
      CostCenter  = var.cost_center
      ExpiresOn   = var.expiry_date
      ManagedBy   = "terraform"
    }
  }
}
