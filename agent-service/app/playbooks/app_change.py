"""Playbook: implement a change request inside one pilot app's folder (any code: templates, CSS, Python, tests)."""
from __future__ import annotations

from app.agents.implement import implement_app_change
from app.agents.triage import Triage
from app.github_ops import GitHubOps
from app.pilot_apps import PilotApp
from app.playbooks.common import build_edits, short
from app.playbooks.guardrails import MAX_CHANGE_LINES, ChangePlan, NeedsHuman


async def plan(gh: GitHubOps, number: str, ticket: dict, t: Triage, app: PilotApp) -> ChangePlan:
    p = await implement_app_change(gh, ticket, app)
    if p.blocked_reason.strip():
        raise NeedsHuman(f"the implementation agent declined: {p.blocked_reason.strip()}")
    _, edits = await build_edits(gh, p.edits, allowed=(f"{app.path}/",),
                                 forbidden={f"{app.path}/requirements.txt"}, limit=MAX_CHANGE_LINES)
    files = ", ".join(e.path for e in edits)
    return ChangePlan(
        title=f"[{number}] {app.name}: {short(p.summary, 60)}",
        body=(f"Automated implementation of ServiceNow **{number}**: {ticket.get('short_description')}\n\n"
              f"**App:** {app.name} (`{app.path}/`)\n\n**What changes:**\n{p.plan}\n\n**Tests:** {p.test_note}\n\n"
              f"**Files:** {files}\n\n`app-ci` runs the tests on this PR; merging deploys via `app-deploy`.\n\n"
              "_Opened by the Incident Flow AI Ops Agent. A human must review and merge._"),
        commit_msg=f"{number}: {app.name}: {short(p.summary, 50)}",
        workflow="app-deploy.yml",
        note=f"Implementation plan ({app.name}): {p.summary}\n{p.plan}\nTests: {p.test_note}",
        edits=edits,
    )
