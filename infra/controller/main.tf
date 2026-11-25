data "azurerm_resource_group" "target" {
  name = var.resource_group_name
}

locals {
  tags           = { environment = "staging", project = "agentic-gitops", purpose = "publisher-controller" }
  container_name = "controller-state"
  state_blob_url = "https://${azurerm_storage_account.state.name}.blob.core.windows.net/${local.container_name}/state.json"
  secret_url     = "https://${var.key_vault_name}.vault.azure.net/secrets/${var.gitlab_secret_name}?api-version=7.4"
  vm_id          = "${data.azurerm_resource_group.target.id}/providers/Microsoft.Compute/virtualMachines/${var.vm_name}"
  aks_id         = "${data.azurerm_resource_group.target.id}/providers/Microsoft.ContainerService/managedClusters/${var.aks_name}"
  secret_scope   = "/subscriptions/${var.subscription_id}/resourceGroups/${var.key_vault_resource_group_name}/providers/Microsoft.KeyVault/vaults/${var.key_vault_name}/secrets/${var.gitlab_secret_name}"
  alerts_enabled = var.alert_email != ""
  workflow_params = {
    subscriptionId   = var.subscription_id
    resourceGroup    = var.resource_group_name
    vmName           = var.vm_name
    aksName          = var.aks_name
    stateBlobUrl     = local.state_blob_url
    secretUrl        = local.secret_url
    gitlabApiUrl     = var.gitlab_api_url
    projectId        = var.gitlab_project_id
    runnerId         = var.gitlab_runner_id
    publisherTag     = var.publisher_tag
    runnerUnit       = var.runner_unit
    identityId       = azurerm_user_assigned_identity.controller.id
    admissionEnabled = var.admission_enabled
    budgetSeconds    = floor(var.monthly_budget_hours * 3600)
  }
}

resource "azurerm_user_assigned_identity" "controller" {
  name                = "id-agentic-gitops-publisher-controller"
  resource_group_name = data.azurerm_resource_group.target.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_storage_account" "state" {
  name                            = var.state_storage_account_name
  resource_group_name             = data.azurerm_resource_group.target.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  default_to_oauth_authentication = true
  tags                            = local.tags

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 7
    }
  }
}

resource "azapi_resource" "state_container" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = local.container_name
  parent_id = "${azurerm_storage_account.state.id}/blobServices/default"

  body = {
    properties = {
      publicAccess = "None"
    }
  }
}

resource "azurerm_role_definition" "vm_lifecycle" {
  name        = "agentic-gitops-controller-vm-lifecycle"
  scope       = local.vm_id
  description = "Read, start, deallocate and run a command on the retained publisher VM only."

  permissions {
    actions = [
      "Microsoft.Compute/virtualMachines/read",
      "Microsoft.Compute/virtualMachines/instanceView/read",
      "Microsoft.Compute/virtualMachines/start/action",
      "Microsoft.Compute/virtualMachines/deallocate/action",
      "Microsoft.Compute/virtualMachines/runCommand/action",
    ]
  }

  assignable_scopes = [local.vm_id]
}

resource "azurerm_role_definition" "aks_lifecycle" {
  name        = "agentic-gitops-controller-aks-lifecycle"
  scope       = local.aks_id
  description = "Read, start and stop the staging AKS cluster only."

  permissions {
    actions = [
      "Microsoft.ContainerService/managedClusters/read",
      "Microsoft.ContainerService/managedClusters/start/action",
      "Microsoft.ContainerService/managedClusters/stop/action",
    ]
  }

  assignable_scopes = [local.aks_id]
}

resource "azurerm_role_assignment" "vm_lifecycle" {
  scope              = local.vm_id
  role_definition_id = azurerm_role_definition.vm_lifecycle.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.controller.principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "aks_lifecycle" {
  scope              = local.aks_id
  role_definition_id = azurerm_role_definition.aks_lifecycle.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.controller.principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_assignment" "state_blob" {
  scope                = azapi_resource.state_container.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "gitlab_secret" {
  count = var.gitlab_secret_access_enabled ? 1 : 0

  scope                = local.secret_scope
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azapi_resource" "workflow" {
  type      = "Microsoft.Logic/workflows@2019-05-01"
  name      = "logic-agentic-gitops-publisher-controller"
  location  = var.location
  parent_id = data.azurerm_resource_group.target.id
  tags      = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.controller.id]
  }

  body = {
    properties = {
      state      = var.workflow_enabled ? "Enabled" : "Disabled"
      definition = jsondecode(file("${path.module}/workflow.json"))
      parameters = { for name, value in local.workflow_params : name => { value = value } }
    }
  }

  depends_on = [
    azurerm_role_assignment.vm_lifecycle,
    azurerm_role_assignment.aks_lifecycle,
    azurerm_role_assignment.state_blob,
    azurerm_role_assignment.gitlab_secret,
  ]
}

resource "azurerm_log_analytics_workspace" "controller" {
  name                = "log-agentic-gitops-publisher-controller"
  resource_group_name = data.azurerm_resource_group.target.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_monitor_diagnostic_setting" "workflow" {
  name                       = "controller-runtime"
  target_resource_id         = azapi_resource.workflow.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.controller.id

  enabled_log {
    category = "WorkflowRuntime"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}

resource "azurerm_monitor_action_group" "controller" {
  count = local.alerts_enabled ? 1 : 0

  name                = "ag-agentic-gitops-publisher-controller"
  resource_group_name = data.azurerm_resource_group.target.name
  short_name          = "gitopsctl"
  tags                = local.tags

  email_receiver {
    name                    = "operator"
    email_address           = var.alert_email
    use_common_alert_schema = true
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "heartbeat" {
  count = local.alerts_enabled && var.workflow_enabled ? 1 : 0

  name                    = "alert-agentic-gitops-controller-heartbeat"
  resource_group_name     = data.azurerm_resource_group.target.name
  location                = var.location
  description             = "No controller run has completed in the last 30 minutes while the workflow is enabled."
  severity                = 1
  evaluation_frequency    = "PT5M"
  window_duration         = "PT30M"
  scopes                  = [azurerm_log_analytics_workspace.controller.id]
  auto_mitigation_enabled = true
  tags                    = local.tags

  criteria {
    query                   = <<-QUERY
      AzureDiagnostics
      | where ResourceProvider == "MICROSOFT.LOGIC" and Category == "WorkflowRuntime"
      | where OperationName == "Microsoft.Logic/workflows/workflowRunCompleted"
      | summarize RunCount = count()
    QUERY
    time_aggregation_method = "Total"
    metric_measure_column   = "RunCount"
    operator                = "LessThan"
    threshold               = 1

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.controller[0].id]
  }
}

resource "azurerm_monitor_metric_alert" "failed_runs" {
  count = local.alerts_enabled ? 1 : 0

  name                = "alert-agentic-gitops-controller-failed-runs"
  resource_group_name = data.azurerm_resource_group.target.name
  scopes              = [azapi_resource.workflow.id]
  description         = "Controller run failed or requested operator attention."
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"

  criteria {
    metric_namespace = "Microsoft.Logic/workflows"
    metric_name      = "RunsFailed"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.controller[0].id
  }

  tags = local.tags
}
