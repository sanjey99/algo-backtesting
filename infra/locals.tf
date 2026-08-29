locals {
  name_prefix = "${var.project}-${var.environment}"

  default_tags = {
    Project     = var.project
    Environment = var.environment
    Owner       = var.owner
    CostCenter  = var.cost_center
    ExpiresOn   = var.expiry_date
    ManagedBy   = "terraform"
  }

  # Producers must tag every expiring object with exactly one of these
  # mutually exclusive retention classes. Untagged objects are intentionally
  # not claimed to receive either expiration policy.
  transient_lifecycle_tag = {
    key   = "LifecycleClass"
    value = "transient"
  }
  selected_public_lifecycle_tag = {
    key   = "LifecycleClass"
    value = "selected-public"
  }
}
