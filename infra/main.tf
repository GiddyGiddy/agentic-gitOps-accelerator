data "azurerm_client_config" "current" {}

locals {
  existing_scope = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.existing_resource_group_name}/providers"
  registry_id    = "${local.existing_scope}/Microsoft.ContainerRegistry/registries/${var.registry_name}"
  model_id       = "${local.existing_scope}/Microsoft.CognitiveServices/accounts/${var.model_resource_name}"
  tags           = { environment = "staging", project = "agentic-gitops" }
}

resource "azurerm_resource_group" "staging" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

resource "azurerm_user_assigned_identity" "control_plane" {
  name                = "id-agentic-gitops-aks-test"
  resource_group_name = azurerm_resource_group.staging.name
  location            = azurerm_resource_group.staging.location
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "kubelet" {
  name                = "id-agentic-gitops-kubelet-test"
  resource_group_name = azurerm_resource_group.staging.name
  location            = azurerm_resource_group.staging.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "control_plane_identity_operator" {
  scope                = azurerm_user_assigned_identity.kubelet.id
  role_definition_name = "Managed Identity Operator"
  principal_id         = azurerm_user_assigned_identity.control_plane.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_kubernetes_cluster" "staging" {
  name                = var.cluster_name
  location            = azurerm_resource_group.staging.location
  resource_group_name = azurerm_resource_group.staging.name
  dns_prefix          = "agentic-gitops-test"
  sku_tier            = "Free"

  local_account_disabled    = true
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  default_node_pool {
    name            = "system"
    vm_size         = var.node_vm_size
    node_count      = 1
    os_disk_size_gb = 32
    os_disk_type    = "Managed"

    upgrade_settings {
      max_surge = "10%"
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.control_plane.id]
  }

  kubelet_identity {
    client_id                 = azurerm_user_assigned_identity.kubelet.client_id
    object_id                 = azurerm_user_assigned_identity.kubelet.principal_id
    user_assigned_identity_id = azurerm_user_assigned_identity.kubelet.id
  }

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  api_server_access_profile {
    authorized_ip_ranges = var.authorized_ip_ranges
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    load_balancer_sku   = "standard"
  }

  dynamic "key_vault_secrets_provider" {
    for_each = var.gitlab_key_vault_enabled ? [1] : []
    content {
      secret_rotation_enabled = false
    }
  }

  tags       = local.tags
  depends_on = [azurerm_role_assignment.control_plane_identity_operator]
}

resource "azurerm_role_assignment" "operator_cluster_admin" {
  scope                = azurerm_kubernetes_cluster.staging.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.operator_object_id
  principal_type       = "User"
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = "id-agentic-gitops-worker-test"
  resource_group_name = azurerm_resource_group.staging.name
  location            = azurerm_resource_group.staging.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "worker" {
  name                      = "gitops-worker"
  user_assigned_identity_id = azurerm_user_assigned_identity.worker.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.staging.oidc_issuer_url
  subject                   = "system:serviceaccount:${var.namespace}:${var.service_account_name}"
}

resource "azurerm_role_assignment" "model_user" {
  scope                = local.model_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "registry_pull" {
  scope                = local.registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.kubelet.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "gitlab_secret_user" {
  count = var.gitlab_key_vault_enabled ? 1 : 0

  scope                = "${local.existing_scope}/Microsoft.KeyVault/vaults/${var.gitlab_key_vault_name}/secrets/${var.gitlab_key_vault_secret_name}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.worker.principal_id
  principal_type       = "ServicePrincipal"

  lifecycle {
    precondition {
      condition     = can(regex("^[A-Za-z0-9-]{3,24}$", var.gitlab_key_vault_name)) && can(regex("^[A-Za-z0-9-]{1,127}$", var.gitlab_key_vault_secret_name))
      error_message = "Provide valid GitLab Key Vault and secret names."
    }
  }
}
