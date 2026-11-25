output "azure_client_id" {
  value = azuread_application.gha.client_id
}

output "azure_tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "azure_subscription_id" {
  value = data.azurerm_client_config.current.subscription_id
}

output "tfstate_resource_group" {
  value = azurerm_resource_group.tfstate.name
}

output "tfstate_account" {
  value = azurerm_storage_account.tfstate.name
}

output "app_backend_config" {
  description = "Arguments for `terraform init` in demo-app-repo/infra."
  value = join(" ", [
    "-backend-config=resource_group_name=${azurerm_resource_group.tfstate.name}",
    "-backend-config=storage_account_name=${azurerm_storage_account.tfstate.name}",
    "-backend-config=container_name=${azurerm_storage_container.tfstate.name}",
    "-backend-config=key=incidentflow.tfstate",
  ])
}

output "github_variables_managed" {
  value = var.manage_github
}
