"""Diagnosis agent: explores the app repo (read-only tools) and proposes a minimal code fix."""
from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel

from app.agents.llm import structured_call
from app.github_ops import GitHubOps
from app.pilot_apps import PilotApp


class ProposedEdit(BaseModel):
    path: str
    old_str: str
    new_str: str


class Diagnosis(BaseModel):
    root_cause: str
    fix_summary: str
    edits: list[ProposedEdit]
    test_note: str


SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "fix_summary": {"type": "string"},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string", "description": "Exact text currently in the file; must occur once. Empty string means append to the end of the file."},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
        "test_note": {"type": "string", "description": "What test was added or changed to cover the fix."},
    },
    "required": ["root_cause", "fix_summary", "edits", "test_note"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "list_files",
        "description": "List file paths in the repository under a prefix (e.g. 'apps/orders/').",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the repository's main branch.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_repo",
        "description": "Search the affected app's files for a regular expression. Returns path:line: text matches.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
]

def system(app: PilotApp) -> str:
    return f"""You are the diagnosis agent for the "{app.name}" Flask app ({app.summary}). Its code is in
{app.path}/ of the GitHub repository, next to the other pilot apps. You are given a ServiceNow incident
describing application errors. Use the tools to read the code, find the root cause, and propose the
smallest safe fix.

Rules:
- Only edit files under {app.path}/. Do not touch other apps, infra/, deploy/ or .github/.
- Keep the change minimal: fix the defect, do not refactor or restyle unrelated code.
- Add or extend a pytest test in {app.path}/tests/ that would have caught the bug.
- Each edit's old_str must be copied exactly from the file and occur exactly once.
When you are done exploring, give your final answer in the required JSON format."""


SEARCHABLE = (".py", ".html", ".css", ".txt", ".tf", ".tfvars", ".yaml", ".yml", ".sh")


def repo_tool_handler(gh: GitHubOps, search_prefix: str):
    """Read-only repository tools for the agents: list / read anywhere, search within search_prefix."""
    async def handle(name: str, args: dict) -> str:
        if name == "list_files":
            files = await asyncio.to_thread(gh.list_files, args["prefix"])
            return "\n".join(files[:200]) or "(no files)"
        if name == "read_file":
            text = await asyncio.to_thread(gh.read_file, args["path"])
            return text[:40_000]
        if name == "search_repo":
            rx = re.compile(args["pattern"])
            hits: list[str] = []
            for path in await asyncio.to_thread(gh.list_files, search_prefix):
                if not path.endswith(SEARCHABLE):
                    continue
                for i, line in enumerate((await asyncio.to_thread(gh.read_file, path)).splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{path}:{i}: {line.strip()}")
            return "\n".join(hits[:100]) or "(no matches)"
        raise ValueError(f"unknown tool {name}")
    return handle


async def diagnose(gh: GitHubOps, ticket: dict, app: PilotApp) -> Diagnosis:
    prompt = (
        f"Incident {ticket.get('number')} ({app.name}): {ticket.get('short_description')}\n\n"
        f"{ticket.get('description') or ''}"
    )
    data = await structured_call(system=system(app), prompt=prompt, schema=SCHEMA, tools=TOOLS,
                                 tool_handler=repo_tool_handler(gh, f"{app.path}/"))
    return Diagnosis.model_validate(data)
