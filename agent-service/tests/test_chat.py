"""Chatbot rules (enforced in code) and the conversation loop, with ServiceNow and Claude faked."""
import json
from types import SimpleNamespace as NS

import pytest

from app import pilot_apps, store
from app.chat import bot
from app.chat.tools import ChatTools, ToolError, is_affirmative


class ChatSN:
    def __init__(self, existing=None):
        self.created: list[tuple[str, dict]] = []
        self.existing = existing or {}

    async def group_id(self, name):
        return "grp-ai-ops"

    async def app_ci_id(self, name):
        return f"ci-{name}"

    async def standard_change_fields(self):
        return {"type": "standard", "chg_model": "std"}

    async def create(self, table, body):
        self.created.append((table, body))
        prefix = "CHG003000" if table == "change_request" else "INC001000"
        return {"number": f"{prefix}{len(self.created)}", "sys_id": f"sys{len(self.created)}"}

    async def find_by_number(self, number, sys_id=None):
        self.lookups = getattr(self, "lookups", []) + [(number, sys_id)]
        return self.existing.get(number)


DRAFT = dict(ticket_type="incident", app_id="inventory", short_description="Stock API returns HTTP 500",
             description="Since 9am every call to /api/stock fails with a 500 error; the whole warehouse team is affected.",
             urgency="high", impact="medium", requested_color="", acceptance_criteria="")


def tools(state=None, sn=None):
    return ChatTools(sn or ChatSN(), state if state is not None else {"turn": 1})


# ---------- app names ----------
def test_pilot_registry_has_three_apps():
    assert [a.id for a in pilot_apps.all_apps()] == ["orders", "inventory", "bookings"]


@pytest.mark.parametrize("name,status,ids", [
    ("Contoso Inventory", "match", ["inventory"]),
    ("the bookings app", "match", ["bookings"]),
    ("inventry", "suggestions", ["inventory"]),
    ("Contoso", "suggestions", ["orders", "inventory", "bookings"]),
    ("Payroll", "not_in_pilot", []),
    ("SAP HR portal", "not_in_pilot", []),
])
def test_app_name_matching(name, status, ids):
    m = pilot_apps.match(name)
    assert m.status == status
    assert ([m.app.id] if m.app else [c.id for c in m.candidates]) == ids


async def test_check_app_name_only_validates_real_matches():
    t = tools()
    assert (await t.call("check_app_name", {"name": "Payroll"}))["status"] == "not_in_pilot"
    assert (await t.call("check_app_name", {"name": "inventry"}))["status"] == "suggestions"
    assert t.state.get("validated_apps", []) == []
    assert (await t.call("check_app_name", {"name": "Inventory"}))["status"] == "match"
    assert t.state["validated_apps"] == ["inventory"]


# ---------- drafting ----------
async def test_draft_requires_validated_app():
    t = tools()
    with pytest.raises(ToolError, match="not been validated"):
        await t.call("draft_ticket", DRAFT)


async def test_draft_rejects_non_pilot_app_id():
    t = tools({"turn": 1, "validated_apps": ["payroll"]})
    with pytest.raises(ToolError, match="not a pilot app"):
        await t.call("draft_ticket", {**DRAFT, "app_id": "payroll"})


@pytest.mark.parametrize("change,err", [
    ({"requested_color": "#0B5FFF"}, "only applies to change requests"),
    ({"ticket_type": "change_request", "requested_color": "blue"}, "hex"),
    ({"short_description": "500"}, "10-120"),
    ({"description": "broken"}, "too thin"),
])
async def test_draft_validation(change, err):
    t = tools({"turn": 1, "validated_apps": ["inventory"]})
    with pytest.raises(ToolError, match=err):
        await t.call("draft_ticket", {**DRAFT, **change})


async def test_draft_creates_card():
    t = tools({"turn": 1, "validated_apps": ["inventory"]})
    out = await t.call("draft_ticket", DRAFT)
    assert out["draft"]["app_name"] == "Contoso Inventory" and t.state["draft"]["status"] == "pending"
    assert t.cards[0]["kind"] == "draft"


# ---------- confirmation gate ----------
async def _drafted(sn=None, **draft):
    state = {"turn": 1, "validated_apps": ["inventory"]}
    t = tools(state, sn)
    out = await t.call("draft_ticket", {**DRAFT, **draft})
    return state, out["draft_id"]


async def test_create_refused_in_same_turn_as_draft():
    sn = ChatSN()
    state, draft_id = await _drafted(sn)
    state["last_user_text"] = "yes"
    with pytest.raises(ToolError, match="not confirmed"):
        await tools(state, sn).call("create_ticket", {"draft_id": draft_id})
    assert sn.created == []


async def test_create_refused_without_a_yes():
    sn = ChatSN()
    state, draft_id = await _drafted(sn)
    state.update(turn=2, last_user_text="yes but make the urgency low")
    with pytest.raises(ToolError, match="not confirmed"):
        await tools(state, sn).call("create_ticket", {"draft_id": draft_id})
    assert sn.created == []


async def test_create_after_typed_yes_and_only_once():
    sn = ChatSN()
    state, draft_id = await _drafted(sn)
    state.update(turn=2, last_user_text="Yes, go ahead")
    t = tools(state, sn)
    out = await t.call("create_ticket", {"draft_id": draft_id})
    assert out["created"]["number"] == "INC0010001"
    table, body = sn.created[0]
    assert table == "incident" and body["cmdb_ci"] == "ci-Contoso Inventory" and body["urgency"] == "1"
    assert body["assignment_group"] == "grp-ai-ops" and "Application: Contoso Inventory" in body["description"]
    assert state["tickets"][0]["number"] == "INC0010001"
    with pytest.raises(ToolError, match="already raised"):
        await tools(state, sn).call("create_ticket", {"draft_id": draft_id})
    assert len(sn.created) == 1


async def test_create_after_confirm_button_change_request():
    sn = ChatSN()
    state, draft_id = await _drafted(sn, ticket_type="change_request", requested_color="#0b5fff",
                                     short_description="Change background colour to brand blue",
                                     acceptance_criteria="The Bookings page background shows #0B5FFF.")
    state["draft"]["confirmed"] = True
    await tools(state, sn).call("create_ticket", {"draft_id": draft_id})
    table, body = sn.created[0]
    assert table == "change_request" and body["type"] == "standard"
    assert "Requested background colour: #0B5FFF" in body["description"]
    assert "Acceptance criteria:" in body["description"]


async def test_change_request_needs_acceptance_criteria():
    t = tools({"turn": 1, "validated_apps": ["inventory"]})
    with pytest.raises(ToolError, match="acceptance criteria"):
        await t.call("draft_ticket", {**DRAFT, "ticket_type": "change_request",
                                      "short_description": "Add a supplier column to the stock table"})
    out = await t.call("draft_ticket", {**DRAFT, "ticket_type": "change_request",
                                        "short_description": "Add a supplier column to the stock table",
                                        "acceptance_criteria": "The stock table shows a Supplier column for every SKU."})
    assert out["draft"]["acceptance_criteria"].startswith("The stock table")


async def test_create_rejects_stale_draft_id():
    sn = ChatSN()
    state, _ = await _drafted(sn)
    state.update(turn=2, last_user_text="yes")
    with pytest.raises(ToolError, match="No such draft"):
        await tools(state, sn).call("create_ticket", {"draft_id": "D0-0000"})


@pytest.mark.parametrize("text,ok", [("yes", True), ("Yep, raise it", True), ("confirm", True), ("ok", True),
                                     ("no", False), ("yes but change the colour", False),
                                     ("what does urgency mean?", False), ("wait", False)])
def test_is_affirmative(text, ok):
    assert is_affirmative(text) is ok


# ---------- status ----------
async def test_status_rejects_bad_number():
    with pytest.raises(ToolError, match="not a valid ticket number"):
        await tools().call("get_ticket_status", {"number": "12345"})


async def test_status_not_found_and_found_with_agent_progress():
    sn = ChatSN({"INC0010042": ("incident", {"number": "INC0010042", "state": "On Hold", "hold_reason": "Awaiting Change",
                                             "assignment_group": "AI Ops", "cmdb_ci": "Contoso Orders"})})
    store.save_ticket(store.Ticket(sys_id="s42", table="incident", number="INC0010042", short_description="x",
                                   status="pr_open", pr_url="https://github.com/o/r/pull/12"))
    t = tools(sn=sn)
    assert (await t.call("get_ticket_status", {"number": "INC0019999"}))["found"] is False
    out = await t.call("get_ticket_status", {"number": "inc0010042"})
    assert out["state"] == "On Hold" and out["ai_agent"]["pull_request"].endswith("/pull/12")


# ---------- conversation loop ----------
def _resp(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks))


def _text(t):
    return NS(type="text", text=t)


def _tool(i, name, inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


def use(monkeypatch, fake):
    monkeypatch.setattr(bot, "client", lambda: fake)
    return fake


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw["messages"])))
        return self.script.pop(0)


async def test_chat_loop_validates_app_and_persists(monkeypatch):
    fake = FakeClaude([
        _resp("tool_use", _tool("t1", "check_app_name", {"name": "payroll"})),
        _resp("end_turn", _text("Payroll is not part of the pilot environment. Which app did you mean?")),
    ])
    monkeypatch.setattr(bot, "client", lambda: fake)
    chat = bot.new_chat()
    out = await bot.respond(chat.id, "payroll is down", ChatSN())
    assert out["entries"][1]["text"].startswith("Payroll is not part of the pilot")
    # the tool result went back to the model
    tool_result = fake.calls[1][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "not_in_pilot" in tool_result["content"]
    view = bot.public_view(store.get_chat(chat.id))
    assert [e["role"] for e in view["transcript"]] == ["assistant", "user", "assistant"]


async def test_chat_loop_end_to_end_create(monkeypatch):
    sn = ChatSN()
    chat = bot.new_chat()
    use(monkeypatch, FakeClaude([
        _resp("tool_use", _tool("t1", "check_app_name", {"name": "inventory"})),
        _resp("tool_use", _tool("t2", "draft_ticket", DRAFT)),
        _resp("end_turn", _text("Here's the draft. Shall I raise it?")),
    ]))
    out = await bot.respond(chat.id, "inventory stock API gives 500s since 9am, whole team affected", sn)
    draft_id = out["draft"]["id"]
    assert out["entries"][1]["cards"][0]["kind"] == "draft"

    # the model tries to create without a confirmation in the same turn: refused
    use(monkeypatch, FakeClaude([
        _resp("tool_use", _tool("t3", "create_ticket", {"draft_id": draft_id})),
        _resp("end_turn", _text("Please confirm the draft first.")),
    ]))
    await bot.respond(chat.id, "what does impact mean?", sn)
    assert sn.created == []

    # Confirm button
    use(monkeypatch, FakeClaude([
        _resp("tool_use", _tool("t4", "create_ticket", {"draft_id": draft_id})),
        _resp("end_turn", _text("Raised INC0010001.")),
    ]))
    out = await bot.respond(chat.id, "", sn, confirm_draft=draft_id)
    assert len(sn.created) == 1 and out["tickets"][0]["number"] == "INC0010001"
    assert out["entries"][0]["text"] == "Yes, please raise it."


async def test_chat_api_failure_is_saved_as_apology(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(bot, "client", lambda: NS(beta=NS(messages=NS(create=boom))))
    chat = bot.new_chat()
    out = await bot.respond(chat.id, "hello", ChatSN())
    assert "try again" in out["entries"][1]["text"]
    history = json.loads(store.get_chat(chat.id).history)
    assert [m["role"] for m in history] == ["user", "assistant"]


async def test_status_uses_exact_record_when_numbers_repeat():
    # PDI sample data reuses CHG numbers: a ticket raised in this chat must be looked up by its sys_id
    sn = ChatSN({"CHG0030006": ("change_request", {"number": "CHG0030006", "state": "Closed"})})
    t = tools({"turn": 1, "tickets": [{"number": "CHG0030006", "sys_id": "ours"}]}, sn)
    await t.call("get_ticket_status", {"number": "CHG0030006"})
    assert sn.lookups == [("CHG0030006", "ours")]
