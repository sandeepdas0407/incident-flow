"""Thin async client for the ServiceNow Table API (incident + change_request)."""
from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("incident_flow.servicenow")

# Incident states
INC_NEW, INC_IN_PROGRESS, INC_ON_HOLD, INC_RESOLVED = "1", "2", "3", "6"
HOLD_AWAITING_CHANGE = "5"

# Change request states
CHG_NEW, CHG_ASSESS, CHG_AUTHORIZE, CHG_SCHEDULED = "-5", "-4", "-3", "-2"
CHG_IMPLEMENT, CHG_REVIEW, CHG_CLOSED = "-1", "0", "3"

TICKET_FIELDS = "sys_id,number,short_description,description,state,priority,category,cmdb_ci,cmdb_ci.name,sys_created_on"
STATUS_FIELDS = ("number,short_description,state,hold_reason,assignment_group,assigned_to,cmdb_ci,"
                 "sys_created_on,sys_updated_on,close_notes")
APP_CI_TABLE = "cmdb_ci_appl"


class ServiceNowError(RuntimeError):
    pass


class ServiceNow:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = client or httpx.AsyncClient(
            base_url=settings.sn_instance_url.rstrip("/"),
            auth=(settings.sn_username, settings.sn_password),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        self._sys_ids: dict[tuple[str, str], str] = {}

    async def close(self) -> None:
        await self.http.aclose()

    # ---------- low level ----------
    async def _get(self, path: str, params: dict) -> list[dict]:
        r = await self.http.get(path, params=params)
        if r.status_code >= 400:
            raise ServiceNowError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json().get("result", [])

    async def _patch(self, table: str, sys_id: str, body: dict) -> dict:
        r = await self.http.patch(f"/api/now/table/{table}/{sys_id}", json=body,
                                  params={"sysparm_exclude_reference_link": "true"})
        if r.status_code >= 400:
            raise ServiceNowError(f"PATCH {table}/{sys_id} -> {r.status_code}: {r.text[:300]}")
        return r.json().get("result", {})

    async def create(self, table: str, body: dict) -> dict:
        r = await self.http.post(f"/api/now/table/{table}", json=body)
        if r.status_code >= 400:
            raise ServiceNowError(f"POST {table} -> {r.status_code}: {r.text[:300]}")
        return r.json()["result"]

    async def lookup_sys_id(self, table: str, field: str, value: str) -> str:
        key = (table, value)
        if key not in self._sys_ids:
            rows = await self._get(f"/api/now/table/{table}",
                                   {"sysparm_query": f"{field}={value}", "sysparm_fields": "sys_id", "sysparm_limit": 1})
            if not rows:
                raise ServiceNowError(f"{table} with {field}={value!r} not found")
            self._sys_ids[key] = rows[0]["sys_id"]
        return self._sys_ids[key]

    async def group_id(self, name: str) -> str:
        return await self.lookup_sys_id("sys_user_group", "name", name)

    async def agent_user_id(self) -> str:
        return await self.lookup_sys_id("sys_user", "user_name", self.s.sn_agent_user)

    async def standard_change_fields(self) -> dict:
        """Fields a new change request needs to run on the Standard (pre-approved) change model:
        New -> Scheduled -> Implement -> Review -> Closed, no CAB approvals."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S"
        return {
            "type": "standard",
            "chg_model": await self.lookup_sys_id("chg_model", "name", "Standard"),
            "start_date": now.strftime(fmt),
            "end_date": (now + timedelta(hours=4)).strftime(fmt),
        }

    async def app_ci_id(self, app_name: str) -> str:
        """sys_id of the pilot app's Configuration Item (created by scripts.setup_servicenow)."""
        return await self.lookup_sys_id(APP_CI_TABLE, "name", app_name)

    async def find_by_number(self, number: str, sys_id: str | None = None) -> tuple[str, dict] | None:
        """Look up a ticket by number with human-readable (display) values, for status questions.

        Numbers are not unique on a PDI: its sample data reuses CHG00300xx numbers. When the caller knows the
        exact record (sys_id) it is used; otherwise the newest record with that number is returned, and
        row["_duplicates"] says how many others share the number."""
        table = "change_request" if number.upper().startswith("CHG") else "incident"
        query = f"number={number.upper()}" + (f"^sys_id={sys_id}" if sys_id else "") + "^ORDERBYDESCsys_created_on"
        rows = await self._get(f"/api/now/table/{table}", {
            "sysparm_query": query, "sysparm_fields": STATUS_FIELDS,
            "sysparm_display_value": "true", "sysparm_exclude_reference_link": "true", "sysparm_limit": 5})
        if not rows and sys_id:
            return await self.find_by_number(number)
        if not rows:
            return None
        return table, {**rows[0], "_duplicates": len(rows) - 1}

    # ---------- intake ----------
    async def new_tickets(self) -> list[tuple[str, dict]]:
        """New incidents / change requests routed to the AI intake group and not yet assigned."""
        grp = await self.group_id(self.s.sn_intake_group)
        out: list[tuple[str, dict]] = []
        for table, state in (("incident", INC_NEW), ("change_request", CHG_NEW)):
            rows = await self._get(f"/api/now/table/{table}", {
                "sysparm_query": f"active=true^state={state}^assignment_group={grp}^assigned_toISEMPTY^ORDERBYsys_created_on",
                "sysparm_fields": TICKET_FIELDS,
                "sysparm_limit": 20,
            })
            out.extend((table, r) for r in rows)
        return out

    async def get(self, table: str, sys_id: str) -> dict:
        rows = await self._get(f"/api/now/table/{table}", {"sysparm_query": f"sys_id={sys_id}", "sysparm_fields": TICKET_FIELDS})
        if not rows:
            raise ServiceNowError(f"{table}/{sys_id} not found")
        return rows[0]

    # ---------- updates ----------
    async def work_note(self, table: str, sys_id: str, note: str) -> None:
        await self._patch(table, sys_id, {"work_notes": note})

    async def claim(self, table: str, sys_id: str, note: str) -> None:
        body = {"assigned_to": await self.agent_user_id(), "work_notes": note}
        if table == "incident":
            body["state"] = INC_IN_PROGRESS
        await self._patch(table, sys_id, body)
        # Change requests (Standard model) stay in New until the PR is open.

    async def set_change_state(self, sys_id: str, state: str, extra: dict | None = None) -> bool:
        """The change model can reject a transition (HTTP 403 from the 'Check State Transition'
        business rule) or silently ignore it. Either way: leave a work note, return False, carry on."""
        try:
            result = await self._patch("change_request", sys_id, {"state": state, **(extra or {})})
            ok = str(result.get("state")) == state
            reason = f"state is still {result.get('state')}"
        except ServiceNowError as e:
            ok, reason = False, str(e)
        if not ok:
            log.warning("change_request %s: transition to %s not applied (%s)", sys_id, state, reason)
            await self.work_note("change_request", sys_id,
                                 f"[AI Ops Agent] Note: the change model did not allow moving to state {state}; continuing.")
        return ok

    async def awaiting_change(self, table: str, sys_id: str, note: str) -> None:
        if table == "incident":
            await self._patch(table, sys_id, {"state": INC_ON_HOLD, "hold_reason": HOLD_AWAITING_CHANGE, "work_notes": note})
        else:
            # Standard (pre-approved) change: Scheduled = ready, pending PR review/merge.
            await self.work_note(table, sys_id, note)
            await self.set_change_state(sys_id, CHG_SCHEDULED)

    async def implementing(self, table: str, sys_id: str, note: str) -> None:
        if table == "incident":
            await self._patch(table, sys_id, {"state": INC_IN_PROGRESS, "work_notes": note})
        else:
            await self.work_note(table, sys_id, note)
            await self.set_change_state(sys_id, CHG_IMPLEMENT)

    async def resolve(self, table: str, sys_id: str, close_notes: str) -> None:
        if table == "incident":
            await self._patch(table, sys_id, {
                "state": INC_RESOLVED,
                "close_code": self.s.sn_incident_close_code,
                "close_notes": close_notes,
                "work_notes": f"[AI Ops Agent] Resolved. {close_notes}",
            })
        else:
            await self.work_note(table, sys_id, f"[AI Ops Agent] Implemented successfully. {close_notes}")
            await self.set_change_state(sys_id, CHG_REVIEW)
            await self.set_change_state(sys_id, CHG_CLOSED, {"close_code": "successful", "close_notes": close_notes})

    async def hand_off(self, table: str, sys_id: str, note: str) -> None:
        body = {"assignment_group": await self.group_id(self.s.sn_human_group), "assigned_to": "", "work_notes": note}
        if table == "incident":
            body["state"] = INC_IN_PROGRESS
        await self._patch(table, sys_id, body)
