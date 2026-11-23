variable "authorized_ip_ranges" {
  description = "Approved IPv4 CIDRs allowed to reach the AKS management API."
  type        = set(string)

  validation {
    condition = length(var.authorized_ip_ranges) > 0 && alltrue([
      for cidr in var.authorized_ip_ranges :
      can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) > 0, false)
    ])
    error_message = "Provide one or more valid IPv4 CIDRs, excluding 0.0.0.0/0."
  }
}

variable "operator_object_id" {
  description = "Microsoft Entra object ID of the approved AKS cluster operator."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.operator_object_id))
    error_message = "Provide the operator's Microsoft Entra object ID as a GUID."
  }
}

variable "location" {
  description = "Azure region for the staging resources."
  type        = string
  default     = "swedencentral"
}

variable "resource_group_name" {
  description = "New resource group for the staging resources."
  type        = string
  default     = "rg-agentic-gitops-test"
}

variable "cluster_name" {
  description = "Name of the AKS cluster."
  type        = string
  default     = "agenticGitOpsTest"
}

variable "node_vm_size" {
  description = "AKS system node SKU; availability and quota must be checked."
  type        = string
  default     = "Standard_D4ls_v5"
}

variable "namespace" {
  description = "Namespace used by the worker service account."
  type        = string
  default     = "gitops-staging"
}

variable "service_account_name" {
  description = "Worker service account name, matching the Helm chart."
  type        = string
  default     = "agent-gitops-bot"
}

variable "existing_resource_group_name" {
  description = "Resource group containing the existing registry and Azure AI resource."
  type        = string
  default     = "rg-inventarmind"
}

variable "registry_name" {
  description = "Existing ACR with legacy registry permissions."
  type        = string
  default     = "inventarmindde4cc0"
}

variable "model_resource_name" {
  description = "Existing Azure AI resource containing the model deployment."
  type        = string
  default     = "gideon-3037-resource"
}

variable "gitlab_key_vault_enabled" {
  description = "Enable the AKS Key Vault CSI provider and GitLab secret access."
  type        = bool
  default     = false
}

variable "gitlab_key_vault_name" {
  description = "Existing Key Vault name for the GitLab token."
  type        = string
  default     = ""
}

variable "gitlab_key_vault_secret_name" {
  description = "Existing Key Vault secret name for the GitLab token."
  type        = string
  default     = ""
}

variable "ui_key_vault_enabled" {
  description = "Enable a dedicated console identity and Key Vault client-secret access."
  type        = bool
  default     = false
}

variable "harbor_key_vault_enabled" {
  description = "Enable a dedicated Harbor identity and secret-scoped Key Vault access."
  type        = bool
  default     = false
}

variable "harbor_key_vault_name" {
  description = "Existing Key Vault name containing Harbor credentials."
  type        = string
  default     = ""

  validation {
    condition     = var.harbor_key_vault_name == "" || can(regex("^[A-Za-z0-9-]{3,24}$", var.harbor_key_vault_name))
    error_message = "Provide a valid Harbor Key Vault name."
  }
}

variable "ui_key_vault_name" {
  description = "Existing Key Vault name containing the Entra client secret."
  type        = string
  default     = ""
}

variable "ui_key_vault_secret_name" {
  description = "Existing Key Vault secret name for the Entra client secret."
  type        = string
  default     = ""
}

variable "ui_service_account_name" {
  description = "Dedicated console service account name, matching the Helm chart."
  type        = string
  default     = "gitops-console"
}

variable "console_query_enabled" {
  description = "Grant the dedicated console identity read access to GitLab credentials and the model for queries."
  type        = bool
  default     = false
}

variable "ui_public_gateway_enabled" {
  description = "Enable the managed Gateway API and a public IP for the staging console."
  type        = bool
  default     = false
}

variable "ui_public_dns_label" {
  description = "Azure public DNS label for the console gateway."
  type        = string
  default     = ""

  validation {
    condition     = var.ui_public_dns_label == "" || can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.ui_public_dns_label))
    error_message = "The Azure DNS label must be lowercase, 3-63 characters, and contain only letters, digits and interior hyphens."
  }
}
