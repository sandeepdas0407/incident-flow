"""Shared helpers for playbooks that apply model-proposed edits."""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess

from app.github_ops import FileEdit, GitHubOps
from app.playbooks.guardrails import GuardrailViolation, apply_str_edit, check_diff_budget, check_path

log = logging.getLogger("incident_flow.playbooks")


def short(text: str, limit: int) -> str:
    """Trim to a word boundary so PR titles and commit messages never end mid-sentence."""
    text = " ".join(text.split()).rstrip(".")
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


async def build_edits(gh: GitHubOps, proposed, allowed: tuple[str, ...], forbidden: set[str],
                      limit: int) -> tuple[dict[str, str], list[FileEdit]]:
    """Validate each proposed edit's path, apply the string edits to the current files (or create new ones),
    and enforce the diff budget. Returns (originals, final edits)."""
    if not proposed:
        raise GuardrailViolation("no edits were proposed")
    originals: dict[str, str] = {}
    current: dict[str, str] = {}
    for e in proposed:
        path = check_path(e.path, allowed)
        if path in forbidden:
            raise GuardrailViolation(f"{path} may not be changed by the agent")
        if path not in current:
            try:
                originals[path] = await asyncio.to_thread(gh.read_file, path)
            except Exception:
                originals[path] = ""  # new file
            current[path] = originals[path]
        current[path] = apply_str_edit(current[path], e.old_str, e.new_str, path)
    edits = [FileEdit(p, c) for p, c in current.items() if c != originals[p]]
    check_diff_budget(originals, edits, limit)
    return originals, edits


def terraform_fmt(path: str, content: str) -> str:
    """Format Terraform with the local CLI when it is available, so the PR passes `terraform fmt -check`."""
    exe = shutil.which("terraform")
    if not exe or not path.endswith((".tf", ".tfvars")):
        return content
    try:
        out = subprocess.run([exe, "fmt", "-"], input=content, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout
        raise GuardrailViolation(f"{path} is not valid Terraform: {out.stderr.strip()[:300]}")
    except subprocess.TimeoutExpired:
        log.warning("terraform fmt timed out for %s", path)
        return content
