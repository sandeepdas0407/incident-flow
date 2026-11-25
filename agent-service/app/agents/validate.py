"""Validation agent: after a change is deployed, check the LIVE system against the ticket before closing it.

Change requests: every acceptance criterion. Incidents: the reported symptom is gone and the app is healthy.
Tools are read-only and fixed: HTTP GETs to the pilot apps only, and one predefined Azure inventory query.
Whether the check passed is computed in code from the per-criterion results, not taken from the model.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app import pilot_apps
from app.agents.diagnose import TOOLS as REPO_TOOLS
from app.agents.diagnose import repo_tool_handler
from app.agents.llm import structured_call
from app.config import get_settings
from app.github_ops import GitHubOps


class Check(BaseModel):
    criterion: str
    result: str  # pass | fail | unverifiable
    evidence: str


class Validation(BaseModel):
    summary: str
    checks: list[Check]
    fixable_in_code: bool
    fix_hint: str

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not any(c.result == "fail" for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.result == "fail"]

    def report(self) -> str:
        icon = {"pass": "PASS", "fail": "FAIL", "unverifiable": "N/A "}
        lines = [f"[{icon.get(c.result, c.result)}] {c.criterion}: {c.evidence}" for c in self.checks]
        return f"{self.summary}\n" + "\n".join(lines)


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences: is the ticket really done?"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string"},
                    "result": {"type": "string", "enum": ["pass", "fail", "unverifiable"]},
                    "evidence": {"type": "string", "description": "What you observed on the live system."},
                },
                "required": ["criterion", "result", "evidence"],
                "additionalProperties": False,
            },
        },
        "fixable_in_code": {"type": "boolean",
                            "description": "True if a further change to the app's code or the Terraform in infra/ "
                                           "(within the pilot rules) could fix every failed check."},
        "fix_hint": {"type": "string", "description": "If anything failed: exactly what still needs to change. "
                                                      "Empty if everything passed."},
    },
    "required": ["summary", "checks", "fixable_in_code", "fix_hint"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "http_get",
        "description": "GET a URL on one of the live pilot apps (only those hosts and ports are allowed). "
                       "Returns the status code, content type and the start of the body.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}},
                         "required": ["url"], "additionalProperties": False},
    },
    {
        "name": "azure_inventory",
        "description": "Read-only snapshot of the live Azure resources for the pilot: every resource in the resource "
                       "group with its tags, the VM size, and the NSG rules.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    *[t for t in REPO_TOOLS if t["name"] in ("list_files", "read_file")],
]


def allowed_url(url: str) -> bool:
    base = urlsplit(get_settings().app_url)
    u = urlsplit(url)
    ports = {a.port for a in pilot_apps.all_apps()}
    return (u.scheme == "http" and bool(base.hostname) and u.hostname == base.hostname
            and (u.port or 80) in ports and not u.username and not u.password)


async def http_get(url: str) -> str:
    if not allowed_url(url):
        raise ValueError("only the pilot app URLs can be checked: " +
                         ", ".join(a.url for a in pilot_apps.all_apps()))
    last = None
    for _ in range(3):  # the app may still be restarting right after a deploy
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as c:
                r = await c.get(url)
            return f"HTTP {r.status_code}\ncontent-type: {r.headers.get('content-type', '')}\n\n{r.text[:6000]}"
        except httpx.HTTPError as e:
            last = e
            await asyncio.sleep(5)
    return f"request failed: {type(last).__name__}: {last}"


def _az(*args: str) -> object:
    exe = shutil.which("az")
    if not exe:
        raise RuntimeError("Azure CLI (az) is not installed on the agent host")
    out = subprocess.run([exe, *args, "-o", "json"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"az {args[0]} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


def azure_inventory_sync() -> str:
    s = get_settings()
    rg = s.azure_resource_group
    data = {
        "resource_group": rg,
        "resources": _az("resource", "list", "-g", rg, "--query", "[].{type:type, name:name, tags:tags}"),
        "resource_group_tags": _az("group", "show", "-n", rg, "--query", "tags"),
        "vm_size": _az("vm", "show", "-g", rg, "-n", s.azure_vm_name, "--query", "hardwareProfile.vmSize"),
        "nsg_rules": _az("network", "nsg", "rule", "list", "-g", rg, "--nsg-name", s.azure_nsg_name, "--query",
                         "[].{name:name, priority:priority, direction:direction, access:access, "
                         "port:destinationPortRange, ports:destinationPortRanges, source:sourceAddressPrefix, "
                         "sources:sourceAddressPrefixes}"),
    }
    return json.dumps(data, indent=1)


def system() -> str:
    apps = "\n".join(f"- {a.name}: {a.url} ({a.summary})" for a in pilot_apps.all_apps())
    return f"""You are the validation agent for an AI operations team. A change for a ServiceNow ticket was approved,
merged and deployed. Before the ticket is closed, decide with evidence from the LIVE system whether it is
really done.

Live pilot apps:
{apps}
Infrastructure: one Azure resource group ({get_settings().azure_resource_group}) holding the shared VM.

How to validate:
- Change request: make one check per acceptance criterion in the ticket (and for the main request itself if
  there are none). Incident: check the reported symptom is gone (e.g. the failing endpoint now returns 2xx with
  sensible data) and the app's page and /health still work.
- Use http_get for app behaviour and azure_inventory for infrastructure. You can read the repository to see
  what was changed, but the evidence must come from the live system.
- Be strict and literal: "every resource" means every resource azure_inventory lists. Do not assume.
- result: pass (evidence shows it holds), fail (evidence shows it does not), unverifiable (your tools cannot
  observe it at all, e.g. a billing report). Never mark something unverifiable just because it failed.
- fixable_in_code: whether a further change to that app's code (its folder only, no new dependencies) or to the
  Terraform in infra/ (no new providers, paid resources, destroy/replace, or internet-open ports) could fix
  every failed check. If a failure needs a manual or out-of-scope action, say false and explain in fix_hint.
When you are done, give your answer in the required JSON format."""


async def validate(gh: GitHubOps, ticket: dict, table: str, scenario: str | None, app_id: str | None,
                   pr_url: str | None) -> Validation:
    repo_handle = repo_tool_handler(gh, "")

    async def handle(name: str, args: dict) -> str:
        if name == "http_get":
            return await http_get(args["url"])
        if name == "azure_inventory":
            return await asyncio.to_thread(azure_inventory_sync)
        return await repo_handle(name, args)

    app = pilot_apps.get(app_id)
    prompt = (f"{'Change request' if table == 'change_request' else 'Incident'} {ticket.get('number')}"
              f" ({app.name if app else 'shared infrastructure'}; playbook {scenario}): "
              f"{ticket.get('short_description')}\n\n{ticket.get('description') or ''}\n\n"
              f"Deployed change: {pr_url}")
    data = await structured_call(system=system(), prompt=prompt, schema=SCHEMA, tools=TOOLS, tool_handler=handle,
                                 max_turns=14)
    return Validation.model_validate(data)
