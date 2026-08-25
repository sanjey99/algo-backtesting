variable "project" {
  description = "Lowercase project slug used in the state bucket prefix and tags."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}[a-z0-9]$", var.project))
    error_message = "project must be a 3-16 character lowercase slug that starts with a letter and ends with a letter or digit."
  }
}

variable "environment" {
  description = "Lowercase deployment environment slug."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,8}[a-z0-9]$", var.environment))
    error_message = "environment must be a 2-10 character lowercase slug that starts with a letter and ends with a letter or digit."
  }
}

variable "owner" {
  description = "Non-secret owner tag for operational accountability."
  type        = string

  validation {
    condition     = var.owner == trimspace(var.owner) && length(var.owner) >= 1 && length(var.owner) <= 64
    error_message = "owner must be nonblank, trimmed, and at most 64 characters."
  }
}

variable "cost_center" {
  description = "Non-secret cost allocation tag."
  type        = string

  validation {
    condition     = var.cost_center == trimspace(var.cost_center) && length(var.cost_center) >= 1 && length(var.cost_center) <= 64
    error_message = "cost_center must be nonblank, trimmed, and at most 64 characters."
  }
}

variable "expiry_date" {
  description = "Calendar date for reviewing and cleaning up the bounded environment."
  type        = string

  validation {
    condition = try(
      formatdate("YYYY-MM-DD", "${var.expiry_date}T00:00:00Z") == var.expiry_date,
      false,
    )
    error_message = "expiry_date must be a real calendar date in YYYY-MM-DD format."
  }
}

variable "region" {
  description = "AWS region that will contain the Terraform state bucket."
  type        = string
  default     = "ap-southeast-1"

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be a valid lowercase AWS region identifier."
  }
}
