"""Deterministic safety checks applied to every change the agents propose."""
from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import dataclass, field

from app.github_ops import FileEdit

ALLOWED_VM_SIZES = ["Standard_B1s", "Standard_B1ms", "Standard_B2s", "Standard_B2ms"]
MAX_DIFF_LINES = 50  # incident fixes: small and surgical
MAX_CHANGE_LINES = 200  # change requests (features, infra)
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class GuardrailViolation(ValueError):
    pass


class NeedsHuman(Exception):
    """A playbook decided (deliberately, not by error) that a person must handle this request."""


@dataclass
class ChangePlan:
    """What a playbook wants to do. The orchestrator turns it into a PR."""
    title: str
    body: str
    commit_msg: str
    workflow: str  # workflow file whose run on the merge commit means "deployed"
    note: str  # diagnosis note for the ticket
    edits: list[FileEdit] = field(default_factory=list)


def check_path(path: str, allowed_prefixes: tuple[str, ...]) -> str:
    norm = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if norm.startswith("..") or not norm.startswith(allowed_prefixes):
        raise GuardrailViolation(f"path {path!r} is outside the allowed scope {allowed_prefixes}")
    return norm


def apply_str_edit(content: str, old: str, new: str, path: str) -> str:
    if old == "":
        return content + ("" if content.endswith("\n") or not content else "\n") + new
    count = content.count(old)
    if count != 1:
        raise GuardrailViolation(f"edit for {path}: old_str must occur exactly once (found {count})")
    return content.replace(old, new, 1)


def changed_lines(before: str, after: str) -> int:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))


def check_diff_budget(originals: dict[str, str], edits: list[FileEdit], limit: int = MAX_DIFF_LINES) -> int:
    total = sum(changed_lines(originals.get(e.path, ""), e.new_content) for e in edits)
    if total == 0:
        raise GuardrailViolation("proposed change is empty")
    if total > limit:
        raise GuardrailViolation(f"proposed change touches {total} lines (limit {limit})")
    return total


# ---------------- infrastructure (Terraform) ----------------
# Resource types that cost nothing on their own. Anything else added by an agent counts as a new paid resource.
FREE_RESOURCE_TYPES = {
    "azurerm_resource_group", "azurerm_virtual_network", "azurerm_subnet", "azurerm_network_security_group",
    "azurerm_network_security_rule", "azurerm_application_security_group", "azurerm_route_table", "azurerm_route",
    "azurerm_network_interface_security_group_association", "azurerm_subnet_network_security_group_association",
    "azurerm_subnet_route_table_association",
}
PUBLIC_SOURCES = {"*", "internet", "any", "0.0.0.0/0", "::/0"}
_RESOURCE = re.compile(r'^\s*resource\s+"([\w-]+)"\s+"([\w-]+)"', re.M)
_RULE_BLOCK = re.compile(r'security_rule\s*\{([^{}]*)\}|resource\s+"azurerm_network_security_rule"\s+"[\w-]+"\s*\{([^{}]*)\}')
_ATTR = re.compile(r'^\s*(\w+)\s*=\s*(.+?)\s*$', re.M)
_MULTIPLY = re.compile(r'^\s*(count|for_each)\s*=', re.M)
_TFVARS_SIZE = re.compile(r'^\s*vm_size\s*=\s*"([^"]+)"', re.M)


def _resources(texts: list[str]) -> set[str]:
    return {f"{m.group(1)}.{m.group(2)}" for t in texts for m in _RESOURCE.finditer(t)}


def _rules(texts: list[str]) -> dict[str, dict]:
    out = {}
    for t in texts:
        for m in _RULE_BLOCK.finditer(t):
            body = m.group(1) if m.group(1) is not None else m.group(2)
            attrs = {k: v.strip().strip('"') for k, v in _ATTR.findall(body)}
            out[" ".join(body.split())] = attrs
    return out


def _is_public_inbound(rule: dict) -> bool:
    if rule.get("direction", "").lower() != "inbound" or rule.get("access", "").lower() != "allow":
        return False
    sources = {rule.get("source_address_prefix", "").lower()}
    sources |= {s.strip().strip('"').lower() for s in re.findall(r'"([^"]*)"', rule.get("source_address_prefixes", ""))}
    return bool(sources & PUBLIC_SOURCES)


def check_infra(originals: dict[str, str], edits: list[FileEdit]) -> None:
    """Static checks on a Terraform change, before it becomes a PR. The terraform-plan check on the PR
    is the second line of defence (it fails on any delete/replace in the real plan)."""
    after = dict(originals)
    after.update({e.path: e.new_content for e in edits})
    tf_before = [v for k, v in originals.items() if k.endswith(".tf")]
    tf_after = [v for k, v in after.items() if k.endswith(".tf")]

    removed = _resources(tf_before) - _resources(tf_after)
    if removed:
        raise GuardrailViolation(f"would delete or replace resources: {', '.join(sorted(removed))}")
    paid = sorted(r for r in _resources(tf_after) - _resources(tf_before) if r.split(".")[0] not in FREE_RESOURCE_TYPES)
    if paid:
        raise GuardrailViolation(f"would add new paid resources: {', '.join(paid)}")
    if len(_MULTIPLY.findall("\n".join(tf_after))) > len(_MULTIPLY.findall("\n".join(tf_before))):
        raise GuardrailViolation("adds count/for_each, which can multiply resources")
    old_rules = _rules(tf_before)
    for key, rule in _rules(tf_after).items():
        if key not in old_rules and _is_public_inbound(rule):
            raise GuardrailViolation(f"opens inbound access from the internet (rule {rule.get('name', '?')!r})")
    for path, text in after.items():
        if path.endswith(".tfvars"):
            for size in _TFVARS_SIZE.findall(text):
                check_vm_size(size)


def check_vm_size(size: str) -> str:
    if size not in ALLOWED_VM_SIZES:
        raise GuardrailViolation(f"vm size {size!r} not in allowlist {ALLOWED_VM_SIZES}")
    return size


def check_color(color: str) -> str:
    if not HEX_COLOR.match(color or ""):
        raise GuardrailViolation(f"colour {color!r} is not a #RRGGBB hex value")
    return color.upper()
