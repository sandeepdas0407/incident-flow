# Incident Flow — AI agents that work ServiceNow tickets through GitOps

A ticket lands in ServiceNow → an AI agent triages it → proposes a fix as a GitHub PR (app code or
Terraform for an Azure VM) → a human approves → GitHub Actions deploys → the agent resolves the ticket.
Every step is written back to the ticket as work notes / state changes and streamed to a live dashboard.

```
ServiceNow PDI ──poll──► agent-service (FastAPI, Claude) ──PR──► GitHub repo ──Actions──► Azure VM
      ▲                        │  dashboard (SSE)                 │ human merge            (Terraform /
      └──── work notes, state ─┴──────────── watches PR + workflow runs ◄──────────────────  app deploy)
```

| Folder | What |
|---|---|
| `agent-service/` | Python agent: service desk chatbot, ServiceNow poller, Claude triage + diagnosis, playbooks with guardrails, GitHub watcher, dashboard |
| `demo-app-repo/` | Its **own** GitHub repo ([incident-flow-demo-app](https://github.com/sandeepdas0407/incident-flow-demo-app)), not included here: three pilot Flask apps (each with a seeded bug), Terraform for the Azure VM, 4 workflows |

## Pilot apps

The pilot environment is exactly three apps, listed in `agent-service/pilot_apps.yaml`. The chatbot only raises tickets for these, and
each playbook only changes files in its own app's folder. All three run on one VM.

| App | Folder | URL | Seeded defect |
|---|---|---|---|
| Contoso Orders | `apps/orders/` | `APP_URL` (port 80) | `/api/orders` 500: divide by zero |
| Contoso Inventory | `apps/inventory/` | `APP_URL:8081` | `/api/stock` 500: SKU without a reorder level |
| Contoso Bookings | `apps/bookings/` | `APP_URL:8082` | `/api/bookings` 500: legacy date format |

## Service desk chatbot

The left side of the dashboard is a chat. Users describe a problem or a change, or ask for a ticket's status. The bot (Claude with
five tools in `app/chat/tools.py`) then:
1. **Checks the app name** against the pilot list. It confirms a match with the user and offers suggestions for typos. Any other app
   gets: "not part of the pilot environment, please re-check the name". If the user insists, **no ticket is raised**.
2. **Asks clarifying questions:**
   - **Incidents:** what's wrong, since when, who's affected.
   - **Change requests:** spec-style questions: what exactly and where, concrete examples (wording, values, hex
     colours, ports and source CIDRs), and **acceptance criteria**. A change request can't be drafted without
     acceptance criteria (enforced in code), and they go into the ticket for the agent to build against.
3. Shows a **draft card**, and raises the ticket in ServiceNow **only after the user confirms** it, through the Confirm button or a "yes"
   typed in a later message. The ticket is linked to the app's Configuration Item and routed to AI Ops, so the agent picks it up.
4. Answers **status** questions from ServiceNow plus the agent's own timeline (PR, deployment).

Rules 1 and 3 are enforced in code, not only in the prompt: a draft needs a validated pilot app, creation needs a confirmed
latest draft, and each draft can only be raised once.

## Demo scenarios (seed script)

| Seed key | ServiceNow record | Agent does | Merge triggers |
|---|---|---|---|
| `vm` | Incident: high CPU/memory | `infra/terraform.tfvars` `vm_size` B1s → B2s | `terraform-plan` on PR, `terraform-apply` on merge |
| `bug` / `inv-bug` / `book-bug` | Incident: API 500 + stack trace | Reads that app's code, fixes the defect, adds a test | `app-ci` on PR, `app-deploy` on merge |
| `color` / `inv-color` / `book-color` | Change Request: new background colour | `apps/<app>/static/style.css` `--app-background` | `app-deploy` on merge |
| `handoff` | Incident: laptop login | Triage notes, reassigns to Service Desk | — |
| `outside-pilot` | Change Request for "Payroll" | Hands off: not a pilot app | — |

**Validation before closing:** after a successful deploy, the validation agent (`app/agents/validate.py`) checks the
**live** system:
- **Incidents:** the symptom is gone and the app is healthy.
- **Change requests:** each acceptance criterion holds.

It uses read-only tools only: HTTP GETs to the three pilot app URLs, nothing else, and a fixed `az` inventory of the
resource group, using the local `az` login. Each criterion gets pass, fail or unverifiable with evidence, which is posted
as a work note. Code decides the verdict: any fail blocks closing.
- **If the check fails and code can fix it,** the agent opens a follow-up fix PR (`incidentflow/<number>-fixN`). Its
  instructions include the validation report, and it still needs your merge.
- **After `MAX_FIX_ATTEMPTS` follow-up fixes (default 2), or when only manual work can fix it,** the ticket goes to
  Service Desk with the report.

**Incident states:** New → In Progress (claimed) → On Hold / Awaiting Change (PR open) → In Progress (merged, deploying) → Resolved.
**Change Request states:** Change Requests are created as **Standard** (pre-approved) changes, so no CAB approval is needed. They move
New → Scheduled (PR open, waiting for review) → Implement (merged, deploying) → Review → Closed, and the GitHub PR review is the peer check.
The **Normal** model would need CAB and assignment-group approvals before it reaches Authorize or Scheduled. If the change model rejects a
transition, the agent leaves a work note and carries on.

**Any change request** for a pilot app or the infrastructure is implemented by an agent (`app_change` / `infra_change`
playbooks, with `app/agents/implement.py`), as a PR a person approves:
- **App changes:** any code inside that app's folder (templates, CSS, Python, tests). Not `requirements.txt`, other apps,
  `deploy/` or CI.
- **Infra changes:** `infra/main.tf`, `variables.tf`, `outputs.tf` and `terraform.tfvars`. Not the backend, the lock file or
  cloud-init. The agent formats the Terraform with `terraform fmt` before opening the PR.
- **Always handed to a person:**
  - security or access changes;
  - deleting data or features;
  - vague requests;
  - destroying or replacing infrastructure;
  - new paid Azure resources;
  - ports opened to the internet;
  - new dependencies.

  Triage flags these, and the orchestrator hands them off in code. The implementation agent can also decline.
- **Infra guardrails in code:**
  - no removed or renamed resources;
  - no new paid resource types;
  - no `count`/`for_each`;
  - no new inbound Allow rule from `*`, `Internet` or `0.0.0.0/0`;
  - VM sizes from the allowlist.

  The `terraform-plan` PR check also fails if the real plan deletes or replaces anything, and then the agent hands the
  ticket off.

**Guardrails:** each playbook can only change specific paths; VM sizes must come from an allowlist; colours must be hex;
a change is capped at 50 changed lines for incident fixes and 200 for change requests; triage confidence must be at least
0.7; the agent **never merges**; if Claude refuses, it hands off.

## Tech stack

ServiceNow PDI (Table API) · Python 3.11+ / FastAPI / httpx / SQLModel (SQLite) · Anthropic SDK (`claude-opus-5-5`, structured outputs,
server-side refusal fallbacks) · PyGithub · Terraform `azurerm ~> 4` with Azure Storage state · GitHub Actions with Azure OIDC ·
Flask + gunicorn + nginx on Ubuntu 24.04 · vanilla JS + Server-Sent Events dashboard · uv.

---

## Setup (once)

### 1. ServiceNow PDI
1. Get a free PDI at https://developer.servicenow.com.
2. Create an integration user (e.g. `incidentflow.integration`) with roles **itil** and **rest_api_explorer**. Use admin for the PDI if you prefer.
3. Fill in `agent-service/.env` (copy `.env.example`), then run:
   ```
   cd agent-service
   uv sync
   uv run python -m scripts.setup_servicenow
   ```
   This creates the **AI Ops** and **Service Desk** groups, the `ai.ops.agent` user, and the three pilot apps as Configuration Items
   (`cmdb_ci_appl`). Creating users and groups needs admin or `user_admin`.
   If the integration user can't, create them in the UI.
4. Run the preflight with `uv run python -m scripts.check_servicenow`. It lists your release's incident close codes; set
   `SN_INCIDENT_CLOSE_CODE` to one of them (on the Brazil release, `Resolved – Permanent Fix Applied`). Then it runs a test incident and
   a test change request through every state transition the agent uses.

### 2. GitHub repo
1. Create a **public** repo (the VM clones it anonymously), e.g. `incident-flow-demo-app`, and push the contents of `demo-app-repo/` to `main`.
2. Create a GitHub environment named `production`.
3. Add branch protection on `main`: require a PR and 1 approval. Don't make `app-ci` or `terraform-plan` *required* checks. They only run
   for changes to their own paths, so a required check would leave PRs for the other path waiting forever.
4. Create a fine-grained PAT for the agent with access to that repo: **Contents RW, Pull requests RW, Actions R, Metadata R**.
   GitHub won't let you approve your own PR. Ideally create the PAT on a separate bot account so the presenter can approve
   on camera. If that isn't possible, merge with "bypass branch protections" as admin.

### 3. Azure (OIDC, no stored secrets)

**With Terraform (recommended):** `azure/bootstrap/` creates everything in this step: state storage, the `incidentflow-gha`
app with its federated credentials, role assignments, the GitHub `production` environment and every repository variable below.
```powershell
az login; gh auth login
cp azure/bootstrap/terraform.tfvars.example azure/bootstrap/terraform.tfvars   # set github_owner, app_dns_label
./azure/deploy.ps1                    # bootstrap (local state) → runs the terraform-apply workflow → prints APP_URL
./azure/destroy.ps1 -KeepBootstrap    # delete the VM and incidentflow-rg only (terraform-destroy workflow)
./azure/destroy.ps1                   # delete everything: app infra first, then state storage and identity
```
The bootstrap keeps its own state in `azure/bootstrap/terraform.tfstate` (gitignored); don't lose it. `destroy.ps1 -Local` destroys
the app infra from your machine instead of GitHub Actions.

**By hand**, the equivalent `az` commands:
```bash
az login
SUB=$(az account show --query id -o tsv)
# Terraform state storage
az group create -n incidentflow-tfstate -l eastus
az storage account create -n <uniquetfstate> -g incidentflow-tfstate --sku Standard_LRS --allow-blob-public-access false
az storage container create -n tfstate --account-name <uniquetfstate> --auth-mode login
# App registration + federated credentials for GitHub Actions
APP=$(az ad app create --display-name incidentflow-gha --query appId -o tsv)
az ad sp create --id $APP
for SUBJ in "repo:<owner>/<repo>:ref:refs/heads/main" "repo:<owner>/<repo>:pull_request" "repo:<owner>/<repo>:environment:production"; do
  az ad app federated-credential create --id $APP --parameters "{\"name\":\"$(echo $SUBJ | tr ':/' '--')\",\"issuer\":\"https://token.actions.githubusercontent.com\",\"subject\":\"$SUBJ\",\"audiences\":[\"api://AzureADTokenExchange\"]}"
done
# GitHub may send subjects in the immutable-ID form instead: repo:<owner>@<owner_id>/<repo>@<repo_id>:...
# If the Azure login fails with AADSTS700213, the error shows the exact subject; add credentials for that form too.
# (Git Bash: run `export MSYS_NO_PATHCONV=1` first, or /subscriptions/... gets rewritten into a Windows path.)
az role assignment create --assignee $APP --role Contributor --scope /subscriptions/$SUB
az role assignment create --assignee $APP --role "Storage Blob Data Contributor" \
  --scope $(az storage account show -n <uniquetfstate> -g incidentflow-tfstate --query id -o tsv)
```
Add these as GitHub **repository variables** (Settings → Secrets and variables → Actions → Variables):

| Variable | Value |
|---|---|
| `AZURE_CLIENT_ID` | `$APP` |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `AZURE_SUBSCRIPTION_ID` | `$SUB` |
| `TFSTATE_RG` / `TFSTATE_ACCOUNT` | `incidentflow-tfstate` / `<uniquetfstate>` |
| `VM_SSH_PUBLIC_KEY` | contents of your `~/.ssh/id_ed25519.pub` |
| `APP_DNS_LABEL` | a globally unique label, e.g. `incidentflow-<yourname>` |
| `APP_RESOURCE_GROUP` / `APP_VM_NAME` | `incidentflow-rg` / `incidentflow-vm` |
| `AZURE_LOCATION` | `centralindia` |

Run **Actions → terraform-apply → Run workflow** once. After about 3–5 minutes, `http://<APP_DNS_LABEL>.centralindia.cloudapp.azure.com` should show
*Contoso Orders* (and `:8081` Inventory, `:8082` Bookings), and each app's API should return 500 (the seeded bugs). Set `APP_URL` in `.env` to
the port-80 address.

## Running the demo
```
cd agent-service
uv run uvicorn app.main:app --port 8080         # dashboard + chatbot: http://localhost:8080
uv run python -m scripts.seed_tickets            # optional: or seed_tickets inv-bug book-color outside-pilot
```
Raise tickets through the **chat** (or the seed script). Then show three things side by side: the **dashboard**, the **ServiceNow
ticket** (work notes) and the **GitHub PR**. Approve and merge each PR; when the workflow finishes, the ticket resolves itself.
Merge PRs one at a time for the clearest story. Merging them all at once also works: a deploy that a newer one replaced still
counts, because the newer deploy contains its change.

**Reset between runs:** `uv run python -m scripts.reset_demo`. It closes agent PRs and opens a reset PR for you to merge, which puts
every pilot app back to its original code, colour and tests, and the VM back to B1s. **Cost:** a B1s/B2s VM costs about $10–30 a month; run `./azure/destroy.ps1 -KeepBootstrap` (or the
**terraform-destroy** workflow, typing `destroy` to confirm) when you're done.

## Tests
```
cd agent-service && uv run pytest -q      # playbook guardrails, ticket lifecycle, Claude loop, ServiceNow payloads (all mocked)
```
