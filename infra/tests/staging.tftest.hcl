mock_provider "azurerm" {
  override_during = plan
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      subscription_id = "00000000-0000-0000-0000-000000000002"
    }
  }

}

variables {
  operator_object_id = "00000000-0000-0000-0000-000000000003"
}

override_resource {
  target          = azurerm_kubernetes_cluster.staging
  override_during = plan
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000002/resourceGroups/test/providers/Microsoft.ContainerService/managedClusters/test"
  }
}

run "secure_cluster_configuration" {
  command = plan

  variables {
    authorized_ip_ranges         = ["203.0.113.1/32"]
    gitlab_key_vault_enabled     = true
    gitlab_key_vault_name        = "test-vault"
    gitlab_key_vault_secret_name = "gitlab-token"
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
