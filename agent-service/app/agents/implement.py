"""Implementation agent: turns a change request into concrete file edits, for one pilot app or for the
Terraform infrastructure. Read-only tools; the playbooks validate every edit before a PR is opened."""
from __future__ import annotations

from pydantic import BaseModel

from app.agents.diagnose import TOOLS, ProposedEdit, repo_tool_handler
from app.agents.llm import structured_call
from app.github_ops import GitHubOps
from app.pilot_apps import PilotApp
from app.playbooks.guardrails import ALLOWED_VM_SIZES, MAX_CHANGE_LINES


class ChangeProposal(BaseModel):
    summary: str
    plan: str
    edits: list[ProposedEdit]
    test_note: str
    blocked_reason: str


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One line: what the change does (used as the PR title)."},
        "plan": {"type": "string", "description": "Short bullet list of what changes, file by file."},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string",
                                "description": "Exact text currently in the file; must occur once. Empty string "
                                               "means append to the end of the file, or create a new file."},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
        "test_note": {"type": "string", "description": "Tests added/updated, or how the change is verified."},
        "blocked_reason": {"type": "string",
                           "description": "Empty if you implemented it. Otherwise why a human must handle it "
                                          "(then edits must be empty)."},
    },
    "required": ["summary", "plan", "edits", "test_note", "blocked_reason"],
    "additionalProperties": False,
}

COMMON = f"""
Hand the request to a human instead (set blocked_reason, no edits) if it:
- touches security or access: authentication, authorisation, permissions, secrets, disabling validation,
  or exposing data that was not exposed before;
- deletes data, pages, endpoints or features;
- is too vague to implement without guessing what the requester wants;
- needs anything outside the files you are allowed to edit (see below).

Otherwise implement it:
- Do exactly what the request and its acceptance criteria ask, nothing more. Follow the existing code style.
- Keep the whole change under {MAX_CHANGE_LINES} changed lines.
- Each edit's old_str must be copied exactly from the current file and occur exactly once. Several edits to the
  same file are applied in order. To create a new file, use old_str "" with the full file as new_str.
When you are done exploring, give your final answer in the required JSON format."""


def app_system(app: PilotApp) -> str:
    return f"""You are the implementation agent for the "{app.name}" Flask app ({app.summary}). Its code is in
{app.path}/ of the GitHub repository, next to the other pilot apps (which you must not touch). You are given a
ServiceNow change request. Read the code with the tools, then propose the edits that implement it.

You may edit only files under {app.path}/: app.py, templates/, static/ and tests/. You may NOT edit
{app.path}/requirements.txt (no new dependencies: use Flask and the Python standard library), other apps,
infra/, deploy/ or .github/. Add or update pytest tests in {app.path}/tests/ for any new or changed behaviour
(the existing tests import the app with `from app import app` and use app.test_client()).
{COMMON}"""


def infra_system() -> str:
    return f"""You are the infrastructure implementation agent. The pilot apps run on one Azure Linux VM defined with
Terraform (azurerm ~> 4) in infra/ of the GitHub repository: main.tf (resource group, VNet, subnet, NSG with
inline security_rule blocks, public IP, NIC, VM), variables.tf, outputs.tf, terraform.tfvars. You are given a
ServiceNow change request. Read the Terraform with the tools, then propose the edits that implement it.

You may edit only infra/main.tf, infra/variables.tf, infra/outputs.tf and infra/terraform.tfvars. Never edit
infra/backend.tf, infra/.terraform.lock.hcl or infra/cloud-init.yaml (cloud-init only runs on first boot).
Also hand the request to a human (blocked_reason) if it needs any of these:
- destroying or replacing a resource (removing or renaming a resource, or changing an attribute that forces
  replacement, such as the VM's name, location, image, admin user or OS disk, or a network's address space);
- a new paid resource (VMs, disks, public IPs, storage, databases, load balancers, ...), or count/for_each;
- opening a port to the internet: an inbound Allow rule from *, Internet or 0.0.0.0/0. Opening a port to a
  specific IP address or CIDR range the requester gave is fine.
VM sizes must be one of {', '.join(ALLOWED_VM_SIZES)} (set vm_size in terraform.tfvars). New NSG rules need a
priority that is not already used. Follow terraform fmt style (two-space indent, aligned "=").
There are no unit tests for Terraform: use test_note to say what the terraform-plan check on the PR will show.
{COMMON}"""


def _prompt(ticket: dict) -> str:
    return (f"Change request {ticket.get('number')}: {ticket.get('short_description')}\n\n"
            f"{ticket.get('description') or ''}")


async def implement_app_change(gh: GitHubOps, ticket: dict, app: PilotApp) -> ChangeProposal:
    data = await structured_call(system=app_system(app), prompt=_prompt(ticket), schema=SCHEMA, tools=TOOLS,
                                 tool_handler=repo_tool_handler(gh, f"{app.path}/"), max_turns=16, max_tokens=20000)
    return ChangeProposal.model_validate(data)


async def implement_infra_change(gh: GitHubOps, ticket: dict) -> ChangeProposal:
    data = await structured_call(system=infra_system(), prompt=_prompt(ticket), schema=SCHEMA, tools=TOOLS,
                                 tool_handler=repo_tool_handler(gh, "infra/"), max_turns=16, max_tokens=20000)
    return ChangeProposal.model_validate(data)
