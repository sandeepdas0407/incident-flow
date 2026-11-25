<#
.SYNOPSIS
  Destroys Incident Flow's Azure resources, in the safe order.

  1. App infra (VM, network, public IP, incidentflow-rg) via the terraform-destroy workflow, or locally with -Local
  2. Bootstrap (state storage, OIDC identity, role assignments, GitHub variables), unless -KeepBootstrap

  The bootstrap holds the app's Terraform state, so it is only destroyed after the app resource group is gone.

.EXAMPLE
  ./azure/destroy.ps1 -KeepBootstrap   # stop paying for the VM, keep everything needed to redeploy
  ./azure/destroy.ps1                  # remove everything
  ./azure/destroy.ps1 -Local           # destroy the app infra from this machine instead of GitHub Actions
#>
param(
    [switch]$KeepBootstrap,
    [switch]$Local,
    [switch]$AutoApprove
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/common.ps1"

Assert-Tools
$repo = Initialize-Env
$bootstrap = Join-Path $PSScriptRoot 'bootstrap'
$appRg = 'incidentflow-rg'

if ((az group exists -n $appRg) -eq 'true') {
    if ($Local) {
        Write-Step "App infra (local terraform destroy)"
        $infra = Join-Path $PSScriptRoot '../demo-app-repo/infra'
        if (-not (Test-Path $infra)) { throw "Clone the demo app repo into demo-app-repo/ for -Local." }
        $backend = (terraform "-chdir=$bootstrap" output -raw app_backend_config) -split ' '
        Invoke-Tf $infra (@('init', '-input=false', '-reconfigure') + $backend)
        # Required inputs without defaults. Their values don't change what gets deleted.
        $destroyArgs = @('destroy', '-input=false',
            '-var=admin_ssh_public_key=unused', '-var=dns_label=unused', '-var=app_repo_url=unused')
        if ($AutoApprove) { $destroyArgs += '-auto-approve' }
        Invoke-Tf $infra $destroyArgs
    }
    else {
        Write-Step "App infra (terraform-destroy workflow in $repo)"
        Write-Host "If the 'production' environment has required reviewers, approve the run in GitHub."
        Invoke-Workflow -Repo $repo -Workflow 'terraform-destroy.yml' -Fields @{ confirm = 'destroy' }
    }
}
else {
    Write-Host "Resource group $appRg not found; app infra already gone."
}

if ($KeepBootstrap) { Write-Host "`nApp infra destroyed. Bootstrap kept; run deploy.ps1 to redeploy."; return }

if ((az group exists -n $appRg) -eq 'true') {
    throw "$appRg still exists. Not destroying the bootstrap, because it holds that resource group's Terraform state."
}

Write-Step "Bootstrap (state storage, OIDC identity, GitHub variables)"
Invoke-Tf $bootstrap @('init', '-input=false')
$destroyArgs = @('destroy', '-input=false')
if ($AutoApprove) { $destroyArgs += '-auto-approve' }
Invoke-Tf $bootstrap $destroyArgs
Write-Host "`nAll Incident Flow Azure resources destroyed."
