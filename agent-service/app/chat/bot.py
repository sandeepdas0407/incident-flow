"""Pilot chatbot: a Claude conversation with the tools in app/chat/tools.py.

The conversation (Claude messages), the transcript the UI shows, and the session state are stored in
SQLite (store.ChatSession), so a page refresh or a service restart doesn't lose the chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets

from app import pilot_apps, store
from app.agents.llm import client
from app.chat.tools import ChatTools, ToolError, tool_defs
from app.config import get_settings

log = logging.getLogger("incident_flow.chat")

MAX_TOOL_ROUNDS = 8
MAX_HISTORY = 120
MAX_INPUT = 2000
_locks: dict[str, asyncio.Lock] = {}


def system_prompt() -> str:
    apps = "\n".join(f"- {a.name}: {a.summary}" for a in pilot_apps.all_apps())
    return f"""You are the Incident Flow service desk assistant for a pilot environment. You help users with
exactly three things: raising incidents, raising change requests, and checking the status of tickets.

The pilot environment has exactly three applications. Tickets can only be raised for these:
{apps}
All three run on one shared Azure VM.

Raising a ticket:
1. Work out whether it is an incident (something is broken or degraded) or a change request (the user wants
   something changed). If it is unclear, ask.
2. Validate the application name, every time:
   - Call check_app_name with the name exactly as the user wrote it.
   - "match": confirm it with the user in plain words ("Just to confirm, this is about Contoso Inventory?")
     and wait for their yes before going further. You can combine that confirmation with your first
     clarifying question. No need to ask again if they already confirmed that app in this chat.
   - "suggestions": ask which one they meant, offering only the suggested pilot apps, then validate their
     answer with check_app_name again.
   - "not_in_pilot": say that the app "is not part of the pilot environment", list the three pilot apps, and
     ask them to re-check the application name. If they confirm it really is a different application, do NOT
     raise a ticket: explain that only the pilot apps are supported here and they should contact the regular
     service desk.
   - Never guess the app or choose one for the user. Never raise a ticket without a validated pilot app.
3. Ask clarifying questions until the request is clear and actionable. Only ask for what you still need, at
   most three short questions per message, and skip anything the user already told you:
   - Incident: what exactly is wrong (page, API, error message), when it started, how many users or what
     business impact, anything that changed recently. Use the answers to set urgency and impact.
   - Change request: any change to one pilot app (text, layout, styling, new or changed pages, API fields,
     validation, business logic) or to the shared Azure infrastructure (firewall rules, VM size, tags, ...)
     can be automated: an AI agent writes the change as a pull request that a person approves before it
     goes live. Ask spec-style questions so it can be built without guessing:
       what exactly should change, and where (which page, section, endpoint or field);
       concrete examples (exact wording, values, columns, formats, colours as #RRGGBB hex: if they name a
       colour, propose a hex and have them confirm it; for firewall changes, the port and the source IP/CIDR);
       acceptance criteria: how they will know it is done (what they should see or be able to do);
       why, and any timing constraints.
     Put the agreed acceptance criteria in the draft. Infrastructure changes still need one of the pilot apps
     as the ticket's application: ask which app needs the change (all three share the VM).
   - Some change requests are still raised but always go to a person instead of the AI agent. Say so up front
     when it applies: security or access (logins, permissions, secrets, exposing data), deleting data or
     features, destroying or replacing infrastructure, new paid Azure resources, opening a port to the whole
     internet, and new software dependencies.
   - Slowness or capacity: mention that the three apps share one VM.
4. When you have what you need, call draft_ticket. The user sees the draft as a card with a Confirm button,
   so do not repeat the draft in your text: one short sentence asking them to confirm or say what to change.
5. Call create_ticket only after the user explicitly confirms the latest draft. If they want changes, call
   draft_ticket again with the updated details.
6. After raising it, give the ticket number and say what happens next: the AI Ops agent triages it; any fix
   is proposed as a pull request that a human must approve before it is deployed; after deployment the agent
   checks the live system (the acceptance criteria, or that the problem is gone) before closing the ticket,
   and proposes a follow-up fix if something is still missing; they can ask you for the status at any time.

Status questions: call get_ticket_status with the number, or list_my_tickets for tickets raised in this chat.
Explain in plain words: the ServiceNow state, who has it, and what the AI agent has done (for example "a fix
is waiting for approval in pull request #12"). Include links when there are any.

Style: friendly and concise; plain text, no markdown headings or tables (a short list is fine). Write ticket
descriptions as clear, complete sentences a support engineer can act on, using only what the user told you;
never invent details. If the user asks for anything outside these three tasks, politely say you can only help
with tickets for the pilot apps."""


def greeting() -> str:
    names = ", ".join(a.name for a in pilot_apps.all_apps())
    return (f"Hi! I'm the Incident Flow assistant for the pilot environment ({names}). I can raise an incident or "
            "a change request for you, or check the status of a ticket. What do you need?")


def _now() -> str:
    return store.utcnow().isoformat()


def new_chat() -> store.ChatSession:
    chat = store.ChatSession(id=secrets.token_hex(8),
                             transcript=json.dumps([{"role": "assistant", "text": greeting(), "ts": _now()}]),
                             state=json.dumps({"turn": 0, "validated_apps": [], "draft": None, "tickets": []}))
    return store.save_chat(chat)


def public_view(chat: store.ChatSession) -> dict:
    state = json.loads(chat.state)
    return {"id": chat.id, "transcript": json.loads(chat.transcript), "draft": state.get("draft"),
            "tickets": state.get("tickets", [])}


def _block_dict(b) -> dict | None:
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return None


async def respond(chat_id: str, text: str, sn, confirm_draft: str | None = None) -> dict:
    lock = _locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        chat = store.get_chat(chat_id)
        if chat is None:
            raise KeyError(chat_id)
        history: list = json.loads(chat.history)
        transcript: list = json.loads(chat.transcript)
        state: dict = json.loads(chat.state)

        text = (text or "").strip()[:MAX_INPUT]
        state["turn"] = state.get("turn", 0) + 1
        draft = state.get("draft")
        if confirm_draft:
            if draft and draft["id"] == confirm_draft and draft["status"] == "pending":
                draft["confirmed"] = True  # the Confirm button on the draft card
            text = text or "Yes, please raise it."
        if not text:
            raise ValueError("empty message")
        if len(history) > MAX_HISTORY:
            reply = "This conversation has become very long. Please start a new chat to continue."
            user_entry = {"role": "user", "text": text, "ts": _now()}
            bot_entry = {"role": "assistant", "text": reply, "cards": [], "ts": _now()}
            chat.transcript = json.dumps(transcript + [user_entry, bot_entry])
            store.save_chat(chat)
            return {"entries": [user_entry, bot_entry], **_tail(state)}
        state["last_user_text"] = text
        user_entry = {"role": "user", "text": text, "ts": _now()}
        history.append({"role": "user", "content": text})

        tools = ChatTools(sn, state)
        try:
            reply = await _run(history, tools)
        except Exception:
            # Keep whatever already happened (a ticket may have been created before the failure),
            # so a retry can't raise it twice.
            log.exception("chat turn failed")
            reply = "Sorry, I couldn't reach the assistant service just now. Please try again in a moment."
            history.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})

        bot_entry = {"role": "assistant", "text": reply, "cards": tools.cards, "ts": _now()}
        chat.history = json.dumps(history)
        chat.transcript = json.dumps(transcript + [user_entry, bot_entry])
        chat.state = json.dumps(state)
        store.save_chat(chat)
        return {"entries": [user_entry, bot_entry], **_tail(state)}


def _tail(state: dict) -> dict:
    return {"draft": state.get("draft"), "tickets": state.get("tickets", [])}


async def _run(history: list, tools: ChatTools) -> str:
    s = get_settings()
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client().beta.messages.create(
            model=s.claude_model,
            max_tokens=4000,
            system=system_prompt(),
            messages=history,
            tools=tool_defs(),
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": s.chat_effort},
        )
        if resp.stop_reason == "refusal":
            reply = ("Sorry, I can't help with that here. I can raise incidents or change requests for the pilot "
                     "apps, or check a ticket's status.")
            history.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
            return reply

        blocks = [d for d in (_block_dict(b) for b in resp.content) if d]
        if blocks:
            history.append({"role": "assistant", "content": blocks})
        if resp.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "tool_use" and tool_uses:
            results = []
            for tu in tool_uses:
                try:
                    out = await tools.call(tu.name, tu.input if isinstance(tu.input, dict) else json.loads(tu.input))
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str)})
                except ToolError as e:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(e), "is_error": True})
                except Exception as e:  # ServiceNow down etc.: let the model tell the user
                    log.exception("chat tool %s failed", tu.name)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": f"The tool failed ({type(e).__name__}). Apologise and suggest trying again shortly."})
            history.append({"role": "user", "content": results})
            continue

        reply = "\n\n".join(b.text for b in resp.content if b.type == "text").strip()
        if not reply:
            reply = "Sorry, I didn't catch that. Could you rephrase?"
            history.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
        return reply

    reply = "Sorry, I got stuck working on that. Could you say it another way?"
    history.append({"role": "assistant", "content": [{"type": "text", "text": reply}]})
    return reply
