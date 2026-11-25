variable "subscription_id" {
  description = "Explicit target subscription ID. Display names are ambiguous in this tenant."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Provide the subscription ID as a GUID."
  }
}

variable "resource_group_name" {
  description = "Existing resource group containing the controlled VM and AKS cluster."
  type        = string
  default     = "rg-agentic-gitops-test"
}

variable "location" {
  description = "Azure region for controller resources."
  type        = string
  default     = "swedencentral"
}

variable "vm_name" {
  description = "Retained runner VM controlled by the workflow."
  type        = string
  default     = "agops-netprobe-vm"
}

variable "aks_name" {
  description = "AKS cluster hosting Harbor, controlled by the workflow."
  type        = string
  default     = "agenticGitOpsTest"
}

variable "state_storage_account_name" {
  description = "Globally unique name for the new controller state storage account."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.state_storage_account_name))
    error_message = "Use 3-24 lowercase letters and digits."
  }
}

variable "key_vault_name" {
  description = "Existing Key Vault that will hold the read-only GitLab controller token."
  type        = string
}

variable "key_vault_resource_group_name" {
  description = "Resource group of the existing Key Vault."
  type        = string
  default     = "rg-inventarmind"
}

variable "gitlab_secret_name" {
  description = "Name of the Key Vault secret holding the GitLab token. This configuration never creates the value."
  type        = string
  default     = "gitops-controller-gitlab-token"
}

variable "gitlab_secret_access_enabled" {
  description = "Grant the identity read access to the GitLab token secret. Enable only after the secret exists."
  type        = bool
  default     = false
}

variable "gitlab_api_url" {
  description = "GitLab REST API base URL without a trailing slash."
  type        = string
  default     = "https://gitlab.cgi-berlin.cloud/api/v4"

  validation {
    condition     = can(regex("^https://[^/]+(/.*[^/])?$", var.gitlab_api_url))
    error_message = "Use an HTTPS URL without a trailing slash."
  }
}

variable "gitlab_project_id" {
  description = "Numeric GitLab project ID (user-reported 2443; verify before use)."
  type        = string
  default     = "2443"

  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.gitlab_project_id))
    error_message = "Use a numeric project ID."
  }
}

variable "gitlab_runner_id" {
  description = "ID of the dedicated image-publisher runner. It is not registered yet."
  type        = number

  validation {
    condition     = var.gitlab_runner_id > 0 && floor(var.gitlab_runner_id) == var.gitlab_runner_id
    error_message = "Use the positive integer ID of the dedicated runner."
  }
}

variable "publisher_tag" {
  description = "Tag carried only by jobs that the dedicated runner may run."
  type        = string
  default     = "harbor-image-publisher"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.publisher_tag))
    error_message = "Use a lowercase GitLab tag."
  }
}

variable "runner_unit" {
  description = "systemd unit started on the VM through Run Command."
  type        = string
  default     = "gitops-image-publisher-runner.service"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+\\.service$", var.runner_unit))
    error_message = "Use a simple systemd service unit name."
  }
}

variable "monthly_budget_hours" {
  description = "Soft monthly compute target in hours, using Europe/Berlin calendar months."
  type        = number
  default     = 20

  validation {
    condition     = var.monthly_budget_hours >= 2 && var.monthly_budget_hours <= 40
    error_message = "Use between 2 and 40 hours."
  }
}

variable "workflow_enabled" {
  description = "Enable the five-minute schedule. Keep false until validation and approval."
  type        = bool
  default     = false
}

variable "admission_enabled" {
  description = "Allow the controller to start compute. Requires the verified GitHub handoff first."
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Recipient for failure alerts. Empty creates no action group or alert rule."
  type        = string
  default     = ""

  validation {
    condition     = var.alert_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "Provide an email address or leave empty."
  }
}
