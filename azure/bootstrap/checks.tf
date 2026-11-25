# Fail at plan time (not halfway through apply) when GitHub management is on but its inputs are missing.
resource "terraform_data" "github_inputs" {
  count = var.manage_github ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.ssh_public_key != ""
      error_message = "manage_github = true but no SSH public key at ${var.ssh_public_key_path}. Run ssh-keygen -t ed25519 or set ssh_public_key_path."
    }
    precondition {
      condition     = var.app_dns_label != ""
      error_message = "manage_github = true needs app_dns_label (globally unique, e.g. incidentflow-<yourname>)."
    }
  }
}
