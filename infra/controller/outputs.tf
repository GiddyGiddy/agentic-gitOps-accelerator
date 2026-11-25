output "workflow_id" {
  description = "Resource ID of the controller workflow."
  value       = azapi_resource.workflow.id
}

output "identity_principal_id" {
  description = "Controller identity principal ID."
  value       = azurerm_user_assigned_identity.controller.principal_id
}

output "state_blob_url" {
  description = "State blob URL. Bootstrap state.json manually before enabling the workflow."
  value       = local.state_blob_url
}
