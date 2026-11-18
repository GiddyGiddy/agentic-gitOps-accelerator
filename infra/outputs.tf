output "resource_group_name" {
  value = azurerm_resource_group.staging.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.staging.name
}

output "worker_identity_client_id" {
  value = azurerm_user_assigned_identity.worker.client_id
}

output "console_identity_client_id" {
  value = var.ui_key_vault_enabled ? azurerm_user_assigned_identity.console[0].client_id : null
}

output "console_gateway_dns_name" {
  value = var.ui_public_gateway_enabled ? azurerm_public_ip.ui_gateway[0].fqdn : null
}

output "registry_login_server" {
  value = "${var.registry_name}.azurecr.io"
}

output "namespace" {
  value = var.namespace
}
