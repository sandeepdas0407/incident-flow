"""Triage agent: classify a ServiceNow ticket into one of the remediation playbooks."""
from __future__ import annotations

from pydantic import BaseModel

from app import pilot_apps
from app.agents.llm import structured_call
from app.playbooks.guardrails import ALLOWED_VM_SIZES

SCENARIOS = ["vm_resize", "app_bugfix", "ui_change", "app_change", "infra_change", "handoff"]

# Reasons a request must go to a person even inside the pilot. Any flag => hand-off (enforced in code).
RISK_FLAGS = {
    "security_or_access": "it touches security or access (authentication, permissions, secrets, data exposure)",
    "deletes_data_or_feature": "it deletes data, pages, endpoints or features",
    "too_vague": "the request is too vague to implement without guessing",
    "infra_destroy_or_replace": "it would destroy or replace infrastructure",
    "infra_new_paid_resource": "it would add a new paid Azure resource",
    "infra_public_port": "it would open a port to the internet",
}


class Triage(BaseModel):
    scenario: str
    confidence: float
    summary: str
    reasoning: str
    target_vm_size: str
    target_color: str
    app: str = "none"
    risk_flags: list[str] = []


def schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "scenario": {"type": "string", "enum": SCENARIOS},
            "confidence": {"type": "number", "description": "0.0 - 1.0"},
            "summary": {"type": "string", "description": "One or two sentences for the ticket work notes."},
            "reasoning": {"type": "string", "description": "Why this playbook fits (or why it needs a human)."},
            "target_vm_size": {"type": "string", "enum": ["", *ALLOWED_VM_SIZES],
                               "description": "For vm_resize only: the size to move to. Empty otherwise."},
            "target_color": {"type": "string",
                             "description": "For ui_change only: requested background colour as #RRGGBB. Empty otherwise."},
            "app": {"type": "string", "enum": [*(a.id for a in pilot_apps.all_apps()), "none"],
                    "description": "Which pilot application the ticket is about; 'none' if it is not one of "
                                   "them or cannot be told from the ticket."},
            "risk_flags": {"type": "array", "items": {"type": "string", "enum": list(RISK_FLAGS)},
                           "description": "Every reason that applies for a human to handle this; empty if none."},
        },
        "required": ["scenario", "confidence", "summary", "reasoning", "target_vm_size", "target_color", "app",
                     "risk_flags"],
        "additionalProperties": False,
    }


def system() -> str:
    apps = "\n".join(f"- {a.id}: {a.name}. {a.summary} Code in {a.path}/." for a in pilot_apps.all_apps())
    return f"""You are the triage agent for an AI operations team. You read ServiceNow incidents and change
requests for the pilot environment: three small Flask apps that share one Azure Linux VM
(Terraform-managed, a small B-series size), all in one GitHub repository:
{apps}

Choose exactly one playbook:
- vm_resize: sustained high CPU or memory pressure on the shared VM, where more capacity is the fix.
  Allowed sizes: Standard_B1s (1 vCPU, 1 GiB), Standard_B1ms (1 vCPU, 2 GiB), Standard_B2s (2 vCPU, 4 GiB),
  Standard_B2ms (2 vCPU, 8 GiB). Pick the smallest allowed size larger than the current one that fixes the
  bottleneck: CPU pressure needs more vCPUs (B1s or B1ms -> Standard_B2s; B2s -> Standard_B2ms), memory-only
  pressure needs more RAM. If the ticket doesn't name the current size, assume Standard_B1s.
  A change request asking for a specific allowed VM size also uses vm_resize.
- app_bugfix: application errors (HTTP 500s, exceptions, stack traces) caused by a code defect in one app.
- ui_change: a change request that ONLY changes one app's background colour. Set target_color as #RRGGBB,
  converting a colour name to a sensible hex value if needed.
- app_change: any other change request for one pilot app's code: text, layout, styling, pages, API fields,
  validation, business logic. The implementation agent edits that app's folder only (no new dependencies).
- infra_change: a change request for the shared Azure infrastructure defined in Terraform (NSG rules, tags,
  outputs, variables, VM settings other than a plain resize).
- handoff: anything else: password resets, access requests, outages that need a person, requests outside
  the pilot apps, or changes to dependencies, deployment scripts or CI.

Set risk_flags to every reason that applies for a person to handle it instead; the playbook will not run:
- security_or_access: authentication, authorisation, permissions, secrets, disabling validation, exposing data.
- deletes_data_or_feature: removing data, pages, endpoints or features.
- too_vague: not enough detail to implement without guessing (no clear what/where/acceptance criteria).
- infra_destroy_or_replace: deleting or recreating infrastructure, including changes that force replacement
  (renaming the VM, moving region, changing the image, address spaces).
- infra_new_paid_resource: adding something that costs money (VMs, disks, public IPs, databases, ...).
- infra_public_port: opening an inbound port to the internet (*, Internet, 0.0.0.0/0). A port opened only to
  a specific IP/CIDR is not this flag.

Set app to the pilot application the ticket is about (the ticket's configuration item, when given, is
authoritative). app_bugfix, ui_change and app_change always need a pilot app; if the ticket is about some
other application, use handoff.

Use confidence honestly: below 0.7 means a human should look first. Keep the summary factual and short;
it is posted on the ticket for the service desk to read."""


async def triage(table: str, ticket: dict) -> Triage:
    prompt = (
        f"Record type: {table}\n"
        f"Number: {ticket.get('number')}\n"
        f"Short description: {ticket.get('short_description')}\n"
        f"Description:\n{ticket.get('description') or '(none)'}\n"
        f"Category: {ticket.get('category') or '(none)'}  Priority: {ticket.get('priority') or '(none)'}\n"
        f"Configuration item: {ticket.get('cmdb_ci.name') or '(none)'}"
    )
    data = await structured_call(system=system(), prompt=prompt, schema=schema(), max_tokens=4000)
    return Triage.model_validate(data)
