"""Playbook: let the diagnosis agent propose a code fix, then validate it before opening a PR."""
from __future__ import annotations

from app.agents.diagnose import Diagnosis, diagnose
from app.agents.triage import Triage
from app.github_ops import FileEdit, GitHubOps
from app.pilot_apps import PilotApp
from app.playbooks.common import build_edits as _build_edits
from app.playbooks.common import short
from app.playbooks.guardrails import MAX_DIFF_LINES, ChangePlan


async def build_edits(gh: GitHubOps, d: Diagnosis, app: PilotApp) -> tuple[dict[str, str], list[FileEdit]]:
    """Bug fixes stay small (MAX_DIFF_LINES) and inside the app's folder; requirements.txt is off-limits."""
    return await _build_edits(gh, d.edits, allowed=(f"{app.path}/",),
                              forbidden={f"{app.path}/requirements.txt"}, limit=MAX_DIFF_LINES)


async def plan(gh: GitHubOps, number: str, ticket: dict, t: Triage, app: PilotApp) -> ChangePlan:
    d = await diagnose(gh, ticket, app)
    _, edits = await build_edits(gh, d, app)
    files = ", ".join(e.path for e in edits)
    return ChangePlan(
        title=f"[{number}] {app.name}: {short(d.fix_summary, 60)}",
        body=(f"Automated remediation for ServiceNow **{number}**: {ticket.get('short_description')}\n\n"
              f"**App:** {app.name} (`{app.path}/`)\n\n**Root cause:** {d.root_cause}\n\n**Fix:** {d.fix_summary}\n\n**Tests:** {d.test_note}\n\n"
              f"**Files:** {files}\n\n`app-ci` runs the tests on this PR; merging deploys via `app-deploy`.\n\n"
              "_Opened by the Incident Flow AI Ops Agent. A human must review and merge._"),
        commit_msg=f"{number}: {app.name}: {short(d.fix_summary, 50)}",
        workflow="app-deploy.yml",
        note=f"Diagnosis ({app.name}): {d.root_cause}\nProposed fix: {d.fix_summary}\nTest: {d.test_note}",
        edits=edits,
    )
