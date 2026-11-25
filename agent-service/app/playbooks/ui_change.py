"""Playbook: change one pilot app's background colour in <app>/static/style.css (deployed on merge)."""
from __future__ import annotations

import asyncio
import re

from app.agents.triage import Triage
from app.pilot_apps import PilotApp
from app.github_ops import FileEdit, GitHubOps
from app.playbooks.guardrails import ChangePlan, GuardrailViolation, check_color, check_diff_budget, check_path

VAR = re.compile(r"(--app-background:\s*)(#[0-9A-Fa-f]{3,8})(\s*;)")


def recolor(content: str, color: str, css: str = "style.css") -> tuple[str, str]:
    m = VAR.search(content)
    if not m:
        raise GuardrailViolation(f"--app-background not found in {css}")
    if m.group(2).upper() == color.upper():
        raise GuardrailViolation(f"background is already {color}")
    return m.group(2), VAR.sub(lambda mm: f"{mm.group(1)}{color}{mm.group(3)}", content, count=1)


async def plan(gh: GitHubOps, number: str, ticket: dict, t: Triage, app: PilotApp) -> ChangePlan:
    path = check_path(f"{app.path}/static/style.css", (f"{app.path}/static/",))
    color = check_color(t.target_color)
    original = await asyncio.to_thread(gh.read_file, path)
    current, updated = recolor(original, color, path)
    edit = FileEdit(path, updated)
    check_diff_budget({path: original}, [edit])
    return ChangePlan(
        title=f"[{number}] {app.name}: background colour {current} -> {color}",
        body=(f"Automated implementation of ServiceNow **{number}**: {ticket.get('short_description')}\n\n"
              f"**App:** {app.name}\n\n**Change:** `--app-background` `{current}` -> `{color}` in `{path}`.\n\n"
              "Merging deploys to the Azure VM via `app-deploy`.\n\n"
              "_Opened by the Incident Flow AI Ops Agent. A human must review and merge._"),
        commit_msg=f"{number}: {app.name} background colour {current} -> {color}",
        workflow="app-deploy.yml",
        note=f"Plan ({app.name}): update --app-background from {current} to {color} in {path}; deploy on approval.",
        edits=[edit],
    )
