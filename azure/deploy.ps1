<#
.SYNOPSIS
  Deploys Incident Flow's Azure resources end to end.

  1. azure/bootstrap   (local Terraform): state storage, OIDC identity, role assignments, GitHub variables
  2. terraform-apply   (GitHub Actions in the demo app repo): VM, network, public IP, apps

.EXAMPLE
  ./azure/deploy.ps1                  # bootstrap + VM
  ./azure/deploy.ps1 -BootstrapOnly   # just the bootstrap
  ./azure/deploy.ps1 -AutoApprove     # no interactive approval for the bootstrap apply
#>
param(
    [switch]$BootstrapOnly,
    [switch]$AutoApprove
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/common.ps1"

Assert-Tools
$repo = Initialize-Env

Write-Step "Bootstrap (state storage, OIDC identity, GitHub variables)"
$tf = Join-Path $PSScriptRoot 'bootstrap'
if (-not (Test-Path (Join-Path $tf 'terraform.tfvars'))) {
    throw "Create $tf/terraform.tfvars from terraform.tfvars.example first."
}
Invoke-Tf $tf @('init', '-input=false', '-upgrade')
$applyArgs = @('apply', '-input=false')
if ($AutoApprove) { $applyArgs += '-auto-approve' }
Invoke-Tf $tf $applyArgs

if ($BootstrapOnly) { Write-Host "`nBootstrap done."; return }

Write-Step "VM and apps (terraform-apply workflow in $repo)"
Invoke-Workflow -Repo $repo -Workflow 'terraform-apply.yml'

$label = (gh variable get APP_DNS_LABEL --repo $repo)
$loc = (gh variable get AZURE_LOCATION --repo $repo)
$url = "http://$label.$loc.cloudapp.azure.com"
Write-Host "`nDeployed. Cloud-init takes ~3-5 minutes more to start the apps:"
Write-Host "  Orders     $url"
Write-Host "  Inventory  ${url}:8081"
Write-Host "  Bookings   ${url}:8082"
Write-Host "Set APP_URL=$url in agent-service/.env"
