variable "project" {
  description = "Lowercase project slug used in resource names and tags."
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
  description = "Calendar date after which the bounded environment should be removed."
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
  description = "AWS region for the research workflow."
  type        = string
  default     = "ap-southeast-1"

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be a valid lowercase AWS region identifier."
  }
}

# Task 10 consumes this approved budget-notification interface.
# tflint-ignore: terraform_unused_declarations
variable "alert_emails" {
  description = "Unique budget-alert recipients; values are operational data, not credentials."
  type        = list(string)

  validation {
    condition = (
      length(var.alert_emails) > 0 &&
      length(var.alert_emails) <= 10 &&
      length(distinct([for email in var.alert_emails : lower(email)])) == length(var.alert_emails) &&
      alltrue([
        for email in var.alert_emails :
        length(email) <= 254 && can(regex("^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$", email))
      ])
    )
    error_message = "alert_emails must contain 1-10 unique, syntactically valid email addresses."
  }
}

# Task 10 consumes this approved immutable runtime-image interface.
# tflint-ignore: terraform_unused_declarations
variable "image_digest" {
  description = "Raw lowercase SHA-256 digest used to pin the runtime image; omit the sha256: prefix."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be exactly 64 lowercase hexadecimal characters without a prefix."
  }
}

# Task 11 consumes this approved OIDC repository interface.
# tflint-ignore: terraform_unused_declarations
variable "github_repository" {
  description = "GitHub repository in owner/name form for later OIDC restrictions."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$", var.github_repository))
    error_message = "github_repository must be a bounded owner/name pair."
  }
}

# Task 11 consumes this approved OIDC ref interface.
# tflint-ignore: terraform_unused_declarations
variable "deploy_ref" {
  description = "Bounded Git ref admitted by the later deployment role."
  type        = string

  validation {
    condition = (
      length(var.deploy_ref) >= 1 &&
      length(var.deploy_ref) <= 255 &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9._/-]*$", var.deploy_ref)) &&
      !strcontains(var.deploy_ref, "..") &&
      !strcontains(var.deploy_ref, "//") &&
      !endswith(var.deploy_ref, "/")
    )
    error_message = "deploy_ref must be a bounded Git ref without traversal, duplicate separators, whitespace, or a trailing slash."
  }
}

# Task 11 consumes this approved protected-environment interface.
# tflint-ignore: terraform_unused_declarations
variable "deploy_environment" {
  description = "Protected GitHub environment admitted by the later deployment role."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", var.deploy_environment))
    error_message = "deploy_environment must be 1-64 safe characters and start with a letter or digit."
  }
}

# Task 10 consumes this approved paid-schedule opt-in interface.
# tflint-ignore: terraform_unused_declarations
variable "enable_schedule" {
  description = "Explicit opt-in for unattended paid execution; false is the safe default."
  type        = bool
  default     = false
}
