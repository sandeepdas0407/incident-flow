variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into. Defaults to ARM_SUBSCRIPTION_ID if null."
  default     = null
}

# --- Terraform state storage -------------------------------------------------------------------------------------

variable "tfstate_resource_group" {
  type    = string
  default = "incidentflow-tfstate"
}

variable "tfstate_location" {
  type    = string
  default = "centralindia"
}

variable "tfstate_account_prefix" {
  type        = string
  description = "Storage account name prefix (lowercase letters/digits, max 18 chars). A random 6-char suffix is appended."
  default     = "ifltfstate"
  validation {
    condition     = can(regex("^[a-z0-9]{3,18}$", var.tfstate_account_prefix))
    error_message = "tfstate_account_prefix must be 3-18 lowercase letters or digits."
  }
}

variable "grant_current_user_state_access" {
  type        = bool
  description = "Give the identity running this bootstrap Storage Blob Data Contributor on the state account (needed for local plan/destroy of the app infra)."
  default     = true
}

# --- GitHub Actions identity ---------------------------------------------------------------------------------------

variable "gha_app_name" {
  type    = string
  default = "incidentflow-gha"
}

variable "github_owner" {
  type        = string
  description = "GitHub user or org that owns the demo app repo."
}

variable "github_repo" {
  type        = string
  description = "Demo app repository name (no owner)."
  default     = "incident-flow-demo-app"
}

variable "extra_federated_subjects" {
  type        = list(string)
  description = <<-EOT
    Additional OIDC subjects. GitHub may send the immutable-ID form, e.g.
    "repo:<owner>@<owner_id>/<repo>@<repo_id>:environment:production". If azure/login fails with AADSTS700213,
    copy the subject from the error into this list.
  EOT
  default     = []
}

# --- GitHub repository configuration -------------------------------------------------------------------------------

variable "manage_github" {
  type        = bool
  description = "Create the `production` environment and the Actions repository variables. Needs GITHUB_TOKEN with admin on the repo."
  default     = true
}

variable "ssh_public_key_path" {
  type        = string
  description = "Public key written to the VM_SSH_PUBLIC_KEY repo variable."
  default     = "~/.ssh/id_ed25519.pub"
}

variable "app_dns_label" {
  type        = string
  description = "Globally unique DNS label for the VM: <label>.<app_location>.cloudapp.azure.com"
  default     = ""
  validation {
    condition     = var.app_dns_label == "" || can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.app_dns_label))
    error_message = "app_dns_label must be lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "app_prefix" {
  type        = string
  description = "Must match `prefix` in demo-app-repo/infra (resource names are <prefix>-rg, <prefix>-vm, ...)."
  default     = "incidentflow"
}

variable "app_location" {
  type    = string
  default = "centralindia"
}
