import json

import httpx
import respx

from app.config import Settings
from app.servicenow import ServiceNow

BASE = "https://dev.example.com"


def make():
    s = Settings(_env_file=None, sn_instance_url=BASE, sn_username="u", sn_password="p")
    return ServiceNow(s, httpx.AsyncClient(base_url=BASE))


@respx.mock
async def test_new_tickets_filters_by_group_and_state():
    respx.get(f"{BASE}/api/now/table/sys_user_group").respond(json={"result": [{"sys_id": "G1"}]})
    inc = respx.get(f"{BASE}/api/now/table/incident").respond(json={"result": [{"sys_id": "i1", "number": "INC1"}]})
    chg = respx.get(f"{BASE}/api/now/table/change_request").respond(json={"result": []})
    rows = await make().new_tickets()
    assert rows == [("incident", {"sys_id": "i1", "number": "INC1"})]
    q = inc.calls[0].request.url.params["sysparm_query"]
    assert "state=1" in q and "assignment_group=G1" in q and "assigned_toISEMPTY" in q
    assert "state=-5" in chg.calls[0].request.url.params["sysparm_query"]


@respx.mock
async def test_resolve_incident_payload():
    route = respx.patch(f"{BASE}/api/now/table/incident/i1").respond(json={"result": {"state": "6"}})
    await make().resolve("incident", "i1", "deployed")
    body = json.loads(route.calls[0].request.content)
    assert body["state"] == "6" and body["close_code"] == "Resolved by change" and body["close_notes"] == "deployed"


@respx.mock
async def test_awaiting_change_sets_on_hold():
    route = respx.patch(f"{BASE}/api/now/table/incident/i1").respond(json={"result": {}})
    await make().awaiting_change("incident", "i1", "PR opened")
    body = json.loads(route.calls[0].request.content)
    assert body["state"] == "3" and body["hold_reason"] == "5"


@respx.mock
async def test_change_transition_veto_is_noted():
    route = respx.patch(f"{BASE}/api/now/table/change_request/c1").mock(side_effect=[
        httpx.Response(200, json={"result": {"state": "-4"}}),  # transition vetoed
        httpx.Response(200, json={"result": {}}),               # work note
    ])
    ok = await make().set_change_state("c1", "-3")
    assert ok is False
    assert "did not allow" in json.loads(route.calls[1].request.content)["work_notes"]


@respx.mock
async def test_change_transition_rejected_with_403_is_not_fatal():
    route = respx.patch(f"{BASE}/api/now/table/change_request/c1").mock(side_effect=[
        httpx.Response(403, json={"error": {"message": "Operation Failed",
                                            "detail": "aborted by Business Rule 'Change Model: Check State Transition'"}}),
        httpx.Response(200, json={"result": {}}),  # work note
    ])
    assert await make().set_change_state("c1", "-2") is False
    assert "did not allow" in json.loads(route.calls[1].request.content)["work_notes"]
