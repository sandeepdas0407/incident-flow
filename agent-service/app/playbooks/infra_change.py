"""Playbook: implement an infrastructure change request in the Terraform under infra/ (applied on merge)."""
from __future__ import annotations

import asyncio

from app.agents.implement import implement_infra_change
from app.agents.triage import Triage
from app.github_ops import FileEdit, GitHubOps
from app.playbooks.common import build_edits, short, terraform_fmt
from app.playbooks.guardrails import MAX_CHANGE_LINES, ChangePlan, NeedsHuman, check_diff_budget, check_infra

ALLOWED_FILES = ("infra/main.tf", "infra/variables.tf", "infra/outputs.tf", "infra/terraform.tfvars")


async def plan(gh: GitHubOps, number: str, ticket: dict, t: Triage, app=None) -> ChangePlan:
    p = await implement_infra_change(gh, ticket)
    if p.blocked_reason.strip():
        raise NeedsHuman(f"the implementation agent declined: {p.blocked_reason.strip()}")
    originals, edits = await build_edits(gh, p.edits, allowed=ALLOWED_FILES, forbidden=set(), limit=MAX_CHANGE_LINES)
    edits = [FileEdit(e.path, terraform_fmt(e.path, e.new_content)) for e in edits]
    # judge the whole configuration, not just the edited files
    for path in ALLOWED_FILES:
        if path not in originals:
            try:
                originals[path] = await asyncio.to_thread(gh.read_file, path)
            except Exception:
                pass
    check_infra(originals, edits)
    check_diff_budget(originals, edits, MAX_CHANGE_LINES)
    files = ", ".join(e.path for e in edits)
    return ChangePlan(
        title=f"[{number}] Infra: {short(p.summary, 64)}",
        body=(f"Automated implementation of ServiceNow **{number}**: {ticket.get('short_description')}\n\n"
              f"**What changes:**\n{p.plan}\n\n**Verification:** {p.test_note}\n\n**Files:** {files}\n\n"
              "`terraform-plan` comments the plan on this PR and fails if anything would be destroyed or replaced. "
              "Merging applies it via `terraform-apply`. The VM hosts all three pilot apps.\n\n"
              "_Opened by the Incident Flow AI Ops Agent. A human must review and merge._"),
        commit_msg=f"{number}: infra: {short(p.summary, 50)}",
        workflow="terraform-apply.yml",
        note=f"Implementation plan (infrastructure): {p.summary}\n{p.plan}\nVerification: {p.test_note}",
        edits=edits,
    )
