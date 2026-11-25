"""Playbook: resize the Azure VM by editing infra/terraform.tfvars (Terraform applies on merge)."""
from __future__ import annotations

import asyncio
import re

from app.agents.triage import Triage
from app.github_ops import FileEdit, GitHubOps
from app.playbooks.guardrails import ChangePlan, GuardrailViolation, check_diff_budget, check_path, check_vm_size

TFVARS = "infra/terraform.tfvars"
LINE = re.compile(r'^(\s*vm_size\s*=\s*")([^"]+)(")', re.M)


def resize_tfvars(content: str, target: str) -> tuple[str, str]:
    m = LINE.search(content)
    if not m:
        raise GuardrailViolation(f"vm_size not found in {TFVARS}")
    current = m.group(2)
    if current == target:
        raise GuardrailViolation(f"VM is already {target}; resizing will not help")
    return current, LINE.sub(lambda mm: f"{mm.group(1)}{target}{mm.group(3)}", content, count=1)


async def plan(gh: GitHubOps, number: str, ticket: dict, t: Triage, app=None) -> ChangePlan:
    # the VM is shared by all pilot apps, so the app (if any) does not change the plan
    path = check_path(TFVARS, ("infra/",))
    target = check_vm_size(t.target_vm_size)
    original = await asyncio.to_thread(gh.read_file, path)
    current, updated = resize_tfvars(original, target)
    edit = FileEdit(path, updated)
    check_diff_budget({path: original}, [edit])
    return ChangePlan(
        title=f"[{number}] Resize VM {current} -> {target}",
        body=(f"Automated remediation for ServiceNow **{number}**: {ticket.get('short_description')}\n\n"
              f"**Triage:** {t.summary}\n\n**Change:** `vm_size` `{current}` -> `{target}` in `{path}`.\n\n"
              "`terraform-plan` will comment the plan on this PR. Merging applies it via `terraform-apply`. "
              "The VM hosts all three pilot apps.\n\n"
              "_Opened by the Incident Flow AI Ops Agent. A human must review and merge._"),
        commit_msg=f"{number}: resize VM {current} -> {target}",
        workflow="terraform-apply.yml",
        note=f"Diagnosis: capacity issue on the VM. Proposed fix: resize {current} -> {target} via Terraform.",
        edits=[edit],
    )
