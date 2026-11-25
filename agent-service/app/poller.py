"""Background loops: ServiceNow intake and GitHub watcher."""
from __future__ import annotations

import asyncio
import logging

from app import orchestrator
from app.config import Settings
from app.github_ops import GitHubOps
from app.servicenow import ServiceNow

log = logging.getLogger("incident_flow.poller")


async def intake_loop(sn: ServiceNow, gh: GitHubOps, s: Settings) -> None:
    while True:
        try:
            for table, row in await sn.new_tickets():
                # sequential on purpose: keeps the demo timeline readable
                await orchestrator.process_new(sn, gh, table, row)
        except Exception:
            log.exception("intake poll failed")
        await asyncio.sleep(s.sn_poll_seconds)


async def watch_loop(sn: ServiceNow, gh: GitHubOps, s: Settings) -> None:
    while True:
        try:
            await orchestrator.watch(sn, gh)
        except Exception:
            log.exception("watch poll failed")
        await asyncio.sleep(s.gh_poll_seconds)
