mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      subscription_id = "00000000-0000-0000-0000-000000000002"
    }
  }

}

mock_provider "azapi" {
  override_during = plan
}

variables {
  operator_object_id        = "00000000-0000-0000-0000-000000000003"
  gitlab_key_vault_enabled  = false
  ui_key_vault_enabled      = false
  harbor_key_vault_enabled  = false
  console_query_enabled     = false
  ui_public_gateway_enabled = false
}

override_resource {
  target          = azurerm_kubernetes_cluster.staging
  override_during = plan
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/test/providers/Microsoft.ContainerService/managedClusters/test"
  }
}

override_resource {
  target          = azurerm_user_assigned_identity.console[0]
  override_during = plan
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/console"
    principal_id = "00000000-0000-0000-0000-000000000004"
  }
}

run "secure_cluster_configuration" {
  command = plan

  variables {
    authorized_ip_ranges         = ["203.0.113.1/32"]
    gitlab_key_vault_enabled     = true
    gitlab_key_vault_name        = "test-vault"
    gitlab_key_vault_secret_name = "gitlab-token"
    ui_key_vault_enabled         = true
    ui_key_vault_name            = "test-vault"
    ui_key_vault_secret_name     = "console-secret"
    console_query_enabled        = true
    ui_public_gateway_enabled    = true
    ui_public_dns_label          = "test-gitops-console"
  }

  assert {
    condition     = azurerm_kubernetes_cluster.staging.sku_tier == "Free" && azurerm_kubernetes_cluster.staging.workload_identity_enabled
    error_message = "Staging must use AKS Free tier and workload identity."
  }

  assert {
    condition     = azurerm_kubernetes_cluster.staging.api_server_access_profile[0].authorized_ip_ranges == toset(["203.0.113.1/32"])
    error_message = "The API server must use the supplied CIDR."
  }

  assert {
    condition     = azurerm_federated_identity_credential.worker.subject == "system:serviceaccount:gitops-staging:agent-gitops-bot"
    error_message = "The federated identity must match the worker service account."
  }

  assert {
    condition = (
      azurerm_role_assignment.operator_cluster_admin.scope == azurerm_kubernetes_cluster.staging.id &&
      azurerm_role_assignment.operator_cluster_admin.role_definition_name == "Azure Kubernetes Service RBAC Cluster Admin" &&
      azurerm_role_assignment.operator_cluster_admin.principal_id == var.operator_object_id &&
      azurerm_role_assignment.operator_cluster_admin.principal_type == "User"
    )
    error_message = "The explicit operator must receive cluster-scoped Azure RBAC Cluster Admin."
  }

  assert {
    condition = (
      length(azurerm_kubernetes_cluster.staging.key_vault_secrets_provider) == 1 &&
      azurerm_role_assignment.gitlab_secret_user[0].role_definition_name == "Key Vault Secrets User" &&
      endswith(azurerm_role_assignment.gitlab_secret_user[0].scope, "/vaults/test-vault/secrets/gitlab-token")
    )
    error_message = "GitLab token access must be enabled and scoped to the specific secret."
  }

  assert {
    condition = (
      azurerm_federated_identity_credential.console[0].subject == "system:serviceaccount:gitops-staging:gitops-console" &&
      azurerm_role_assignment.ui_secret_user[0].role_definition_name == "Key Vault Secrets User" &&
      endswith(azurerm_role_assignment.ui_secret_user[0].scope, "/vaults/test-vault/secrets/console-secret") &&
      azurerm_role_assignment.ui_secret_user[0].principal_id == azurerm_user_assigned_identity.console[0].principal_id
    )
    error_message = "Console secret access must use the dedicated console identity and secret scope."
  }

  assert {
    condition = (
      azurerm_role_assignment.console_gitlab_secret_user[0].role_definition_name == "Key Vault Secrets User" &&
      endswith(azurerm_role_assignment.console_gitlab_secret_user[0].scope, "/vaults/test-vault/secrets/gitlab-token") &&
      azurerm_role_assignment.console_gitlab_secret_user[0].principal_id == azurerm_user_assigned_identity.console[0].principal_id &&
      azurerm_role_assignment.console_model_user[0].role_definition_name == "Cognitive Services OpenAI User" &&
      azurerm_role_assignment.console_model_user[0].scope == local.model_id &&
      azurerm_role_assignment.console_model_user[0].principal_id == azurerm_user_assigned_identity.console[0].principal_id
    )
    error_message = "Console query access must use the console identity and narrowly scoped GitLab and model roles."
  }

  assert {
    condition = (
      azurerm_public_ip.ui_gateway[0].allocation_method == "Static" &&
      azurerm_public_ip.ui_gateway[0].sku == "Standard" &&
      azurerm_public_ip.ui_gateway[0].domain_name_label == "test-gitops-console"
    )
    error_message = "Console ingress must use a static Standard public IP with the configured DNS label."
  }
}

run "query_access_disabled_by_default" {
  command = plan

  variables {
    authorized_ip_ranges = ["203.0.113.1/32"]
  }

  assert {
    condition     = length(azurerm_role_assignment.console_gitlab_secret_user) == 0 && length(azurerm_role_assignment.console_model_user) == 0
    error_message = "Console query roles must be absent unless explicitly enabled."
  }
}

run "harbor_access_is_secret_scoped" {
  command = plan

  variables {
    authorized_ip_ranges     = ["203.0.113.1/32"]
    harbor_key_vault_enabled = true
    harbor_key_vault_name    = "test-vault"
  }

  assert {
    condition = (
      azurerm_federated_identity_credential.harbor[0].subject == "system:serviceaccount:harbor:harbor-secret-sync" &&
      length(azurerm_role_assignment.harbor_secret_user) == 11 &&
      alltrue([
        for name, role in azurerm_role_assignment.harbor_secret_user :
        role.role_definition_name == "Key Vault Secrets User" &&
        endswith(role.scope, "/vaults/test-vault/secrets/${name}")
      ])
    )
    error_message = "Harbor must use its dedicated service account and secret-scoped access."
  }
}

run "harbor_access_requires_vault" {
  command = plan

  variables {
    authorized_ip_ranges     = ["203.0.113.1/32"]
    harbor_key_vault_enabled = true
    harbor_key_vault_name    = ""
  }

  expect_failures = [azurerm_user_assigned_identity.harbor[0]]
}

run "query_access_requires_console_and_gitlab_configuration" {
  command = plan

  variables {
    authorized_ip_ranges  = ["203.0.113.1/32"]
    console_query_enabled = true
  }

  expect_failures = [
    azurerm_role_assignment.console_gitlab_secret_user[0],
    azurerm_role_assignment.console_model_user[0],
  ]
}

run "reject_invalid_operator" {
  command = plan

  variables {
    authorized_ip_ranges = ["203.0.113.1/32"]
    operator_object_id   = "not-a-guid"
  }

  expect_failures = [var.operator_object_id]
}

run "reject_empty_api_access" {
  command = plan

  variables {
    authorized_ip_ranges = []
  }

  expect_failures = [var.authorized_ip_ranges]
}

run "reject_public_api_access" {
  command = plan

  variables {
    authorized_ip_ranges = ["0.0.0.0/0"]
  }

  expect_failures = [var.authorized_ip_ranges]
}
