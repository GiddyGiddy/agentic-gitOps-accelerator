output "resource_group_name" {
  value = azurerm_resource_group.staging.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.staging.name
}

output "worker_identity_client_id" {
  value = azurerm_user_assigned_identity.worker.client_id
}

output "registry_login_server" {
  value = "${var.registry_name}.azurecr.io"
}

output "namespace" {
  value = var.namespace
}
