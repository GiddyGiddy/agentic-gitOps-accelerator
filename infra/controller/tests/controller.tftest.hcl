mock_provider "azurerm" {
  override_during = plan

  mock_data "azurerm_resource_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-test"
    }
  }
}

mock_provider "azapi" {
  override_during = plan
}

override_resource {
  target          = azurerm_user_assigned_identity.controller
  override_during = plan
  values = {
    id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/controller"
    principal_id = "00000000-0000-0000-0000-000000000002"
  }
}

override_resource {
  target          = azurerm_storage_account.state
  override_during = plan
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/statetest"
  }
}

override_resource {
  target          = azapi_resource.state_container
  override_during = plan
  values = {
    id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-test/providers/Microsoft.Storage/storageAccounts/statetest/blobServices/default/containers/controller-state"
  }
}

override_resource {
  target          = azurerm_role_definition.vm_lifecycle
  override_during = plan
  values = {
    role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000001/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000003"
  }
}

override_resource {
  target          = azurerm_role_definition.aks_lifecycle
  override_during = plan
  values = {
    role_definition_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000001/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000004"
  }
}

variables {
  subscription_id            = "00000000-0000-0000-0000-000000000001"
  state_storage_account_name = "statetest"
  key_vault_name             = "kv-test"
  gitlab_runner_id           = 7
  alert_email                = ""
  gitlab_secret_access_enabled = false
}

run "safe_defaults" {
  command = plan

  assert {
    condition     = azapi_resource.workflow.body.properties.state == "Disabled"
    error_message = "The workflow must be disabled by default."
  }

  assert {
    condition     = azapi_resource.workflow.body.properties.parameters.admissionEnabled.value == false
    error_message = "Starting compute must be disabled by default."
  }

  assert {
    condition     = azapi_resource.workflow.body.properties.parameters.budgetSeconds.value == 72000
    error_message = "The default soft target is 20 hours."
  }

  assert {
    condition     = length(azurerm_role_assignment.gitlab_secret) == 0
    error_message = "Secret access must stay off until the secret exists."
  }

  assert {
    condition     = length(azurerm_monitor_action_group.controller) == 0 && length(azurerm_monitor_metric_alert.failed_runs) == 0
    error_message = "Alerts require an explicit recipient."
  }
}

run "least_privilege_scopes" {
  command = plan

  variables {
    gitlab_secret_access_enabled = true
  }

  assert {
    condition     = azurerm_role_definition.vm_lifecycle.scope == azurerm_role_assignment.vm_lifecycle.scope && endswith(azurerm_role_assignment.vm_lifecycle.scope, "/virtualMachines/agops-netprobe-vm")
    error_message = "VM permissions must be scoped to the retained VM."
  }

  assert {
    condition     = endswith(azurerm_role_assignment.aks_lifecycle.scope, "/managedClusters/agenticGitOpsTest")
    error_message = "AKS permissions must be scoped to the cluster."
  }

  assert {
    condition     = endswith(azurerm_role_assignment.gitlab_secret[0].scope, "/vaults/kv-test/secrets/gitops-controller-gitlab-token") && azurerm_role_assignment.gitlab_secret[0].role_definition_name == "Key Vault Secrets User"
    error_message = "Secret access must be scoped to one secret."
  }

  assert {
    condition     = endswith(azurerm_role_assignment.state_blob.scope, "/containers/controller-state") && azurerm_role_assignment.state_blob.role_definition_name == "Storage Blob Data Contributor"
    error_message = "State access must be scoped to one container."
  }

  assert {
    condition     = !contains(azurerm_role_definition.vm_lifecycle.permissions[0].actions, "Microsoft.Compute/virtualMachines/write") && !contains(azurerm_role_definition.vm_lifecycle.permissions[0].actions, "Microsoft.Compute/virtualMachines/delete")
    error_message = "The VM role must not modify or delete the VM."
  }
}

run "storage_is_entra_only" {
  command = plan

  assert {
    condition     = !azurerm_storage_account.state.shared_access_key_enabled && !azurerm_storage_account.state.allow_nested_items_to_be_public && azurerm_storage_account.state.min_tls_version == "TLS1_2"
    error_message = "State storage must use Entra authentication, no public blobs and TLS 1.2."
  }
}

run "alerts_with_recipient" {
  command = plan

  variables {
    alert_email      = "operator@example.com"
    workflow_enabled = true
  }

  assert {
    condition     = length(azurerm_monitor_action_group.controller) == 1 && length(azurerm_monitor_metric_alert.failed_runs) == 1
    error_message = "A recipient must enable the alert path."
  }

  assert {
    condition     = length(azurerm_monitor_scheduled_query_rules_alert_v2.heartbeat) == 1 && azurerm_monitor_scheduled_query_rules_alert_v2.heartbeat[0].criteria[0].threshold == 1
    error_message = "An enabled workflow must have a heartbeat alert."
  }
}

run "no_heartbeat_alert_while_disabled" {
  command = plan

  variables {
    alert_email = "operator@example.com"
  }

  assert {
    condition     = length(azurerm_monitor_scheduled_query_rules_alert_v2.heartbeat) == 0
    error_message = "A disabled workflow must not raise heartbeat alerts."
  }
}

run "rejects_unsafe_inputs" {
  command = plan

  variables {
    gitlab_runner_id = 0
    runner_unit      = "x.service; reboot"
  }

  expect_failures = [var.gitlab_runner_id, var.runner_unit]
}
