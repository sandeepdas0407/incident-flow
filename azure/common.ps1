# Shared helpers for deploy.ps1 / destroy.ps1.

function Write-Step([string]$Text) {
    Write-Host "`n==> $Text" -ForegroundColor Cyan
}

function Assert-Tools {
    foreach ($tool in 'terraform', 'az', 'gh') {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not installed or not on PATH." }
    }
    az account show --only-show-errors *> $null
    if ($LASTEXITCODE -ne 0) { throw "Not logged in to Azure. Run: az login" }
    gh auth status *> $null
    if ($LASTEXITCODE -ne 0) { throw "Not logged in to GitHub. Run: gh auth login" }
}

# Sets the env vars the providers need and returns "<owner>/<repo>" from bootstrap/terraform.tfvars.
function Initialize-Env {
    if (-not $env:ARM_SUBSCRIPTION_ID) { $env:ARM_SUBSCRIPTION_ID = (az account show --query id -o tsv) }
    if (-not $env:GITHUB_TOKEN) { $env:GITHUB_TOKEN = (gh auth token) }

    $tfvars = Join-Path $PSScriptRoot 'bootstrap/terraform.tfvars'
    if (-not (Test-Path $tfvars)) { throw "Create $tfvars from terraform.tfvars.example first." }
    $content = Get-Content $tfvars -Raw
    $owner = [regex]::Match($content, '(?m)^\s*github_owner\s*=\s*"([^"]+)"').Groups[1].Value
    $name = [regex]::Match($content, '(?m)^\s*github_repo\s*=\s*"([^"]+)"').Groups[1].Value
    if (-not $name) { $name = 'incident-flow-demo-app' }
    if (-not $owner) { throw "github_owner is not set in $tfvars." }
    Write-Host "Subscription: $(az account show --query name -o tsv) ($env:ARM_SUBSCRIPTION_ID)"
    Write-Host "Repository:   $owner/$name"
    return "$owner/$name"
}

function Invoke-Tf([string]$Dir, [string[]]$TfArgs) {
    terraform "-chdir=$Dir" @TfArgs
    if ($LASTEXITCODE -ne 0) { throw "terraform $($TfArgs[0]) failed in $Dir" }
}

# Dispatches a workflow, waits for that run to finish, and throws if it failed.
function Invoke-Workflow {
    param([string]$Repo, [string]$Workflow, [hashtable]$Fields = @{})
    $since = (Get-Date).ToUniversalTime().AddSeconds(-5)
    $fieldArgs = foreach ($k in $Fields.Keys) { '-f'; "$k=$($Fields[$k])" }
    gh workflow run $Workflow --repo $Repo --ref main @fieldArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not start $Workflow (is it pushed to main?)" }

    $runId = $null
    for ($i = 0; $i -lt 30 -and -not $runId; $i++) {
        Start-Sleep -Seconds 2
        $runs = gh run list --repo $Repo --workflow $Workflow --event workflow_dispatch --limit 5 `
            --json databaseId,createdAt | ConvertFrom-Json
        $runId = ($runs | Where-Object { ([datetime]$_.createdAt).ToUniversalTime() -ge $since } |
            Select-Object -First 1).databaseId
    }
    if (-not $runId) { throw "Started $Workflow but couldn't find the run. Check the Actions tab." }

    Write-Host "Run: https://github.com/$Repo/actions/runs/$runId"
    gh run watch $runId --repo $Repo --exit-status
    if ($LASTEXITCODE -ne 0) { throw "$Workflow failed. See https://github.com/$Repo/actions/runs/$runId" }
}
