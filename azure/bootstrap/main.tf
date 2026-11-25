# Bootstrap for Incident Flow: everything the app's Terraform (demo-app-repo/infra) and its GitHub Actions
# need before they can run. Replaces the manual `az` steps in README "Setup → 3. Azure".
#
#   - Terraform state storage (resource group, storage account, container)
#   - Entra app registration + service principal for GitHub Actions, with OIDC federated credentials
#   - Role assignments: Contributor on the subscription, Storage Blob Data Contributor on the state account
#   - (optional) GitHub `production` environment and the repository variables the workflows read
#
# This configuration keeps its own state LOCALLY (it creates the remote state storage, so it can't live there).
# Keep terraform.tfstate safe; it's gitignored.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Authenticates with your local `az login`.
provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

provider "azuread" {}

# Reads the token from the GITHUB_TOKEN environment variable (or `gh auth token`).
# Only used when manage_github = true.
provider "github" {
  owner = var.github_owner
}

data "azurerm_client_config" "current" {}
data "azurerm_subscription" "current" {}

locals {
  repo_slug = "${var.github_owner}/${var.github_repo}"

  federated_subjects = concat(
    [
      "repo:${local.repo_slug}:ref:refs/heads/main",
      "repo:${local.repo_slug}:pull_request",
      "repo:${local.repo_slug}:environment:production",
    ],
    var.extra_federated_subjects,
  )

  tags = {
    project = "incident-flow-demo"
    purpose = "bootstrap"
  }
}

# ---------------------------------------------------------------------------------------------------------------
# Terraform state storage
# ---------------------------------------------------------------------------------------------------------------

resource "random_string" "tfstate" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "tfstate" {
  name     = var.tfstate_resource_group
  location = var.tfstate_location
  tags     = local.tags
}

resource "azurerm_storage_account" "tfstate" {
  name                            = "${var.tfstate_account_prefix}${random_string.tfstate.result}"
  resource_group_name             = azurerm_resource_group.tfstate.name
  location                        = azurerm_resource_group.tfstate.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  tags                            = local.tags

  blob_properties {
    # Lets you recover a state file after a bad write or an accidental delete.
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
    container_delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "tfstate" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.tfstate.id
  container_access_type = "private"
}

# ---------------------------------------------------------------------------------------------------------------
# GitHub Actions identity (OIDC, no stored secrets)
# ---------------------------------------------------------------------------------------------------------------

resource "azuread_application" "gha" {
  display_name = var.gha_app_name
  owners       = [data.azurerm_client_config.current.object_id]
}

resource "azuread_service_principal" "gha" {
  client_id = azuread_application.gha.client_id
  owners    = [data.azurerm_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "gha" {
  for_each = toset(local.federated_subjects)

  application_id = azuread_application.gha.id
  display_name   = substr(replace(each.value, "/[^A-Za-z0-9-]/", "-"), 0, 120)
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = each.value
  audiences      = ["api://AzureADTokenExchange"]
}

# The app Terraform creates its own resource group, so the CI identity needs subscription scope.
resource "azurerm_role_assignment" "gha_contributor" {
  scope                = data.azurerm_subscription.current.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.gha.object_id
}

# The backend uses Entra auth (use_azuread_auth = true), not account keys.
resource "azurerm_role_assignment" "gha_state" {
  scope                = azurerm_storage_account.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.gha.object_id
}

# Lets you run `terraform plan/destroy` for the app locally with your own `az login`.
resource "azurerm_role_assignment" "me_state" {
  count = var.grant_current_user_state_access ? 1 : 0

  scope                = azurerm_storage_account.tfstate.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

# ---------------------------------------------------------------------------------------------------------------
# GitHub repository configuration (optional)
# ---------------------------------------------------------------------------------------------------------------

locals {
  # try(): HCL evaluates both sides of the conditional below, so a missing key must not fail when manage_github = false.
  ssh_public_key = try(trimspace(file(pathexpand(var.ssh_public_key_path))), "")

  github_variables = var.manage_github ? {
    AZURE_CLIENT_ID       = azuread_application.gha.client_id
    AZURE_TENANT_ID       = data.azurerm_client_config.current.tenant_id
    AZURE_SUBSCRIPTION_ID = data.azurerm_client_config.current.subscription_id
    TFSTATE_RG            = azurerm_resource_group.tfstate.name
    TFSTATE_ACCOUNT       = azurerm_storage_account.tfstate.name
    VM_SSH_PUBLIC_KEY     = local.ssh_public_key
    APP_DNS_LABEL         = var.app_dns_label
    APP_RESOURCE_GROUP    = "${var.app_prefix}-rg"
    APP_VM_NAME           = "${var.app_prefix}-vm"
    AZURE_LOCATION        = var.app_location
  } : {}
}

resource "github_repository_environment" "production" {
  count = var.manage_github ? 1 : 0

  repository  = var.github_repo
  environment = "production"
}

resource "github_actions_variable" "this" {
  for_each = local.github_variables

  repository    = var.github_repo
  variable_name = each.key
  value         = each.value
}
