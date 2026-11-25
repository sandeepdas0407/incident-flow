"""Chatbot tools. The pilot rules live here, in code, not only in the prompt:

1. A ticket can only be drafted for a pilot app that check_app_name validated in this chat.
2. A ticket is only created from the latest draft, after the user confirmed it (Confirm button,
   or a plain "yes" typed in a later message than the one that produced the draft).
3. Each draft can be raised once.
4. Status lookups need a well-formed ticket number.
"""
from __future__ import annotations

import logging
import re
import secrets

from app import pilot_apps, store
from app.config import get_settings
from app.playbooks.guardrails import HEX_COLOR

log = logging.getLogger("incident_flow.chat")

LEVELS = {"high": "1", "medium": "2", "low": "3"}
NUMBER = re.compile(r"^(INC|CHG)\d{7}$", re.I)
AFFIRM = re.compile(
    r"^\s*(yes|yep|yeah|yup|y|sure|ok|okay|confirm(ed)?|correct|go ahead|proceed|please do|do it|raise it|"
    r"submit( it)?|looks good|lgtm|that'?s (right|correct|fine)|all good|perfect)\b", re.I)
NEGATE = re.compile(r"\b(no|not|don'?t|wait|change|but|instead|edit|cancel|hold on|actually)\b", re.I)


class ToolError(ValueError):
    """Returned to the model as an error tool_result, so it can correct course."""


def is_affirmative(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 80 and bool(AFFIRM.match(t)) and not NEGATE.search(t)


def tool_defs() -> list[dict]:
    app_ids = [a.id for a in pilot_apps.all_apps()]
    level = {"type": "string", "enum": list(LEVELS)}
    return [
        {
            "name": "check_app_name",
            "description": ("Validate an application name the user gave against the pilot environment. Returns "
                            "status 'match' (with the app), 'suggestions' (close pilot apps to ask the user about) or "
                            "'not_in_pilot'. Call it with the user's own wording every time they name an app."),
            "strict": True,
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                             "required": ["name"], "additionalProperties": False},
        },
        {
            "name": "draft_ticket",
            "description": ("Prepare an incident or change request for the user to review. Only for a pilot app that "
                            "check_app_name matched and the user confirmed. The user sees the draft as a card with a "
                            "Confirm button. Calling it again replaces the previous draft."),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "ticket_type": {"type": "string", "enum": ["incident", "change_request"]},
                    "app_id": {"type": "string", "enum": app_ids},
                    "short_description": {"type": "string", "description": "One line, 10-120 characters."},
                    "description": {"type": "string",
                                    "description": "Full details from the conversation, in clear sentences."},
                    "urgency": level,
                    "impact": level,
                    "requested_color": {"type": "string",
                                        "description": "Change requests for a background colour only: #RRGGBB. "
                                                       "Empty string otherwise."},
                    "acceptance_criteria": {"type": "string",
                                            "description": "Change requests: how the requester will know it is done, "
                                                           "as short concrete checks agreed with the user. Empty "
                                                           "string for incidents."},
                },
                "required": ["ticket_type", "app_id", "short_description", "description", "urgency", "impact",
                             "requested_color", "acceptance_criteria"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_ticket",
            "description": "Raise the confirmed draft in ServiceNow. Only after the user explicitly confirmed it.",
            "strict": True,
            "input_schema": {"type": "object", "properties": {"draft_id": {"type": "string"}},
                             "required": ["draft_id"], "additionalProperties": False},
        },
        {
            "name": "get_ticket_status",
            "description": ("Current status of a ticket by number (INC... or CHG...): ServiceNow state, who has it, "
                            "and the AI agent's progress (pull request, deployment)."),
            "strict": True,
            "input_schema": {"type": "object", "properties": {"number": {"type": "string"}},
                             "required": ["number"], "additionalProperties": False},
        },
        {
            "name": "list_my_tickets",
            "description": "Tickets raised in this chat, with their current status.",
            "strict": True,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


class ChatTools:
    def __init__(self, sn, state: dict):
        self.sn = sn
        self.state = state
        self.cards: list[dict] = []  # shown in the chat UI under the assistant's reply

    async def call(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolError(f"unknown tool {name}")
        return await handler(**args)

    # ---------- app validation ----------
    async def _t_check_app_name(self, name: str) -> dict:
        m = pilot_apps.match(name)
        pilot = [a.name for a in pilot_apps.all_apps()]
        if m.status == "match":
            validated = self.state.setdefault("validated_apps", [])
            if m.app.id not in validated:
                validated.append(m.app.id)
            return {"status": "match", "app": m.app.public(),
                    "next": "Confirm this app name with the user before continuing."}
        if m.status == "suggestions":
            return {"status": "suggestions", "you_said": name, "candidates": [a.name for a in m.candidates],
                    "next": "Ask the user which of these pilot apps they mean, then validate their answer again."}
        return {"status": "not_in_pilot", "you_said": name, "pilot_apps": pilot,
                "next": (f"Tell the user '{name}' is not part of the pilot environment, list the pilot apps, and ask "
                         "them to re-check the name. Do not raise a ticket for an app outside the pilot.")}

    # ---------- drafting ----------
    async def _t_draft_ticket(self, ticket_type: str, app_id: str, short_description: str, description: str,
                              urgency: str, impact: str, requested_color: str, acceptance_criteria: str = "") -> dict:
        app = pilot_apps.get(app_id)
        if app is None:
            raise ToolError(f"'{app_id}' is not a pilot app. No ticket can be raised for it.")
        if app_id not in self.state.get("validated_apps", []):
            raise ToolError("This app has not been validated in this chat yet: call check_app_name with the name the "
                            "user gave and confirm it with them first.")
        if ticket_type not in ("incident", "change_request"):
            raise ToolError("ticket_type must be incident or change_request")
        if urgency not in LEVELS or impact not in LEVELS:
            raise ToolError("urgency and impact must be high, medium or low")
        short_description = " ".join(short_description.split())
        if not 10 <= len(short_description) <= 120:
            raise ToolError("short_description must be 10-120 characters")
        if len(description.strip()) < 20:
            raise ToolError("description is too thin: ask the user for the missing details first")
        color = requested_color.strip()
        if color:
            if ticket_type != "change_request":
                raise ToolError("requested_color only applies to change requests")
            if not HEX_COLOR.match(color):
                raise ToolError("requested_color must be a #RRGGBB hex value: confirm the exact colour with the user")
            color = color.upper()
        criteria = acceptance_criteria.strip()
        if ticket_type == "change_request" and len(criteria) < 15:
            raise ToolError("Change requests need acceptance criteria: ask the user how they will know the change is "
                            "done (what they should see or be able to do), then draft again.")
        draft = {
            "id": f"D{self.state.get('turn', 0)}-{secrets.token_hex(2)}",
            "created_turn": self.state.get("turn", 0),
            "status": "pending",
            "confirmed": False,
            "ticket_type": ticket_type,
            "app_id": app.id,
            "app_name": app.name,
            "short_description": short_description,
            "description": description.strip()[:4000],
            "urgency": urgency,
            "impact": impact,
            "requested_color": color,
            "acceptance_criteria": criteria[:2000] if ticket_type == "change_request" else "",
        }
        self.state["draft"] = draft
        self.cards.append({"kind": "draft", **draft})
        return {"draft_id": draft["id"], "draft": draft,
                "next": "The user sees this draft as a card. Ask them to confirm it or say what to change. "
                        "Do not call create_ticket until they confirm in their next message."}

    def _confirmed(self, draft: dict) -> bool:
        if draft.get("confirmed"):
            return True
        return self.state.get("turn", 0) > draft["created_turn"] and is_affirmative(self.state.get("last_user_text", ""))

    # ---------- creation ----------
    async def _t_create_ticket(self, draft_id: str) -> dict:
        draft = self.state.get("draft")
        if not draft or draft["id"] != draft_id:
            raise ToolError("No such draft. Call draft_ticket first, show it to the user and get their confirmation.")
        if draft["status"] != "pending":
            raise ToolError(f"This draft was already raised as {draft.get('number')}. Do not raise it twice.")
        if not self._confirmed(draft):
            raise ToolError("The user has not confirmed this draft yet. Ask them to confirm the draft card "
                            "(or tell you what to change) before raising it.")
        s = get_settings()
        app = pilot_apps.get(draft["app_id"])
        table = draft["ticket_type"]
        details = draft["description"]
        if draft["requested_color"]:
            details += f"\n\nRequested background colour: {draft['requested_color']}"
        if draft.get("acceptance_criteria"):
            details += f"\n\nAcceptance criteria:\n{draft['acceptance_criteria']}"
        body = {
            "short_description": f"{app.name}: {draft['short_description']}"
            if app.name.lower() not in draft["short_description"].lower() else draft["short_description"],
            "description": f"{details}\n\nApplication: {app.name}\nRaised via the Incident Flow pilot chatbot.",
            "assignment_group": await self.sn.group_id(s.sn_intake_group),
            "urgency": LEVELS[draft["urgency"]],
            "impact": LEVELS[draft["impact"]],
        }
        try:
            body["cmdb_ci"] = await self.sn.app_ci_id(app.name)
        except Exception:  # CI missing (setup not run): the Application line in the description still names it
            log.warning("no configuration item for %s; raising without cmdb_ci", app.name)
        if table == "change_request":
            body.update(await self.sn.standard_change_fields())
        rec = await self.sn.create(table, body)
        draft.update(status="created", number=rec["number"])
        ticket = {"number": rec["number"], "table": table, "sys_id": rec["sys_id"], "app": app.name,
                  "short_description": body["short_description"],
                  "url": f"{s.sn_instance_url.rstrip('/')}/nav_to.do?uri={table}.do?sys_id={rec['sys_id']}"}
        self.state.setdefault("tickets", []).append(ticket)
        self.cards.append({"kind": "ticket", **ticket})
        return {"created": ticket,
                "next": "Tell the user the number and what happens next. They can ask for the status any time."}

    # ---------- status ----------
    async def _t_get_ticket_status(self, number: str) -> dict:
        number = number.strip().upper()
        if not NUMBER.match(number):
            raise ToolError("That is not a valid ticket number. Incidents look like INC0010012 and change "
                            "requests like CHG0030005. Ask the user to check it.")
        # prefer the exact record we know (raised in this chat, or handled by the agent): numbers can repeat
        t = store.ticket_by_number(number)
        known = next((x["sys_id"] for x in self.state.get("tickets", []) if x["number"] == number), None)
        found = await self.sn.find_by_number(number, known or (t.sys_id if t else None))
        if not found:
            return {"found": False, "number": number, "next": "Tell the user no ticket with this number exists."}
        table, row = found
        out = {
            "found": True,
            "number": row.get("number"),
            "type": "change request" if table == "change_request" else "incident",
            "short_description": row.get("short_description"),
            "state": row.get("state"),
            "on_hold_reason": row.get("hold_reason") or None,
            "assignment_group": row.get("assignment_group") or None,
            "assigned_to": row.get("assigned_to") or None,
            "application": row.get("cmdb_ci") or None,
            "opened": row.get("sys_created_on"),
            "last_updated": row.get("sys_updated_on"),
            "resolution_notes": row.get("close_notes") or None,
        }
        if row.get("_duplicates") and not (known or t):
            out["note"] = (f"{row['_duplicates']} other record(s) share this number in ServiceNow; this is the newest. "
                           "Mention this if the details don't match what the user expects.")
        if t:
            out["ai_agent"] = {
                "status": t.status, "playbook": t.scenario, "pull_request": t.pr_url, "deployment_run": t.run_url,
                "recent_steps": [f"{e.stage}: {e.message[:300]}" for e in store.events_for(t.sys_id)[-4:]],
            }
        else:
            out["ai_agent"] = None
        return out

    async def _t_list_my_tickets(self) -> dict:
        mine = self.state.get("tickets", [])
        if not mine:
            return {"tickets": [], "next": "No tickets were raised in this chat. Offer to look one up by number."}
        out = []
        for t in mine:
            try:
                st = await self._t_get_ticket_status(t["number"])
                out.append({k: st.get(k) for k in ("number", "type", "short_description", "state", "assignment_group")}
                           | {"ai_agent_status": (st.get("ai_agent") or {}).get("status")})
            except Exception as e:
                out.append({"number": t["number"], "error": str(e)})
        return {"tickets": out}
