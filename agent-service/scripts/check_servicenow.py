"""Preflight: run the agent's exact ServiceNow calls against your PDI and report what works.

Creates one test incident and one test change request (NOT routed to the AI Ops group, so a
running agent will never pick them up), walks each through the agent's lifecycle, and reports
the state after every step.

    uv run python -m scripts.check_servicenow
"""
import asyncio
import sys

from app.config import get_settings
from app.servicenow import ServiceNow, ServiceNowError

INC_STATES = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
CHG_STATES = {"-5": "New", "-4": "Assess", "-3": "Authorize", "-2": "Scheduled", "-1": "Implement",
              "0": "Review", "3": "Closed", "4": "Canceled"}
problems: list[str] = []


def line(ok: bool, msg: str) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {msg}")
    if not ok:
        problems.append(msg)


async def walk(sn: ServiceNow, table: str, sys_id: str, steps, labels: dict) -> None:
    for name, call, expected in steps:
        try:
            await call()
            state = str((await sn.get(table, sys_id))["state"])
            line(state == expected, f"{name:<16} -> {labels.get(state, state)}"
                 + ("" if state == expected else f" (expected {labels.get(expected, expected)})"))
        except ServiceNowError as e:
            line(False, f"{name:<16} -> error: {e}")


async def main() -> int:
    s = get_settings()
    sn = ServiceNow(s)
    print(f"ServiceNow: {s.sn_instance_url}  (user: {s.sn_username})\n")
    try:
        print("1. Connection")
        try:
            await sn._get("/api/now/table/sys_user", {"sysparm_query": f"user_name={s.sn_username}", "sysparm_limit": 1})
            line(True, "authenticated")
        except ServiceNowError as e:
            line(False, f"cannot authenticate: {e}")
            print("\nCheck SN_INSTANCE_URL / SN_USERNAME / SN_PASSWORD in .env (and that the PDI is awake).")
            return 1

        print("\n2. Groups and agent user (created by scripts.setup_servicenow)")
        for label, coro in [(f"group '{s.sn_intake_group}'", sn.group_id(s.sn_intake_group)),
                            (f"group '{s.sn_human_group}'", sn.group_id(s.sn_human_group)),
                            (f"user '{s.sn_agent_user}'", sn.agent_user_id())]:
            try:
                await coro
                line(True, label)
            except ServiceNowError:
                line(False, f"{label} missing; run: uv run python -m scripts.setup_servicenow")
        if problems:
            return 1

        print("\n3. Incident close codes on this instance")
        choices = await sn._get("/api/now/table/sys_choice", {
            "sysparm_query": "name=incident^element=close_code^inactive=false", "sysparm_fields": "label,value"})
        values = sorted({c["value"] for c in choices})
        print(f"     available: {', '.join(values) or '(none found)'}")
        line(s.sn_incident_close_code in values or not values,
             f"SN_INCIDENT_CLOSE_CODE='{s.sn_incident_close_code}' is a valid choice")

        print("\n4. Incident lifecycle")
        inc = await sn.create("incident", {"short_description": "[Incident Flow preflight] test incident - safe to ignore",
                                           "urgency": "3", "impact": "3"})
        print(f"     created {inc['number']}")
        i = inc["sys_id"]
        await walk(sn, "incident", i, [
            ("claim", lambda: sn.claim("incident", i, "[preflight] claim"), "2"),
            ("awaiting change", lambda: sn.awaiting_change("incident", i, "[preflight] PR opened"), "3"),
            ("implementing", lambda: sn.implementing("incident", i, "[preflight] merged"), "2"),
            ("resolve", lambda: sn.resolve("incident", i, "[preflight] deployed"), "6"),
        ], INC_STATES)

        print("\n5. Change request lifecycle (Standard change model)")
        chg = await sn.create("change_request", {
            "short_description": "[Incident Flow preflight] test change - safe to ignore",
            "description": "Preflight check of the agent's change request state transitions.",
            # routed to the human group so a running agent never picks it up
            "assignment_group": await sn.group_id(s.sn_human_group),
            "risk": "4", "impact": "3",
            **await sn.standard_change_fields(),
        })
        print(f"     created {chg['number']}")
        c = chg["sys_id"]
        await walk(sn, "change_request", c, [
            ("claim", lambda: sn.claim("change_request", c, "[preflight] claim"), "-5"),
            ("awaiting change", lambda: sn.awaiting_change("change_request", c, "[preflight] PR opened"), "-2"),
            ("implementing", lambda: sn.implementing("change_request", c, "[preflight] merged"), "-1"),
            ("resolve", lambda: sn.resolve("change_request", c, "[preflight] deployed"), "3"),
        ], CHG_STATES)

        print(f"\n{'All checks passed.' if not problems else f'{len(problems)} problem(s):'}")
        for p in problems:
            print(f"  - {p}")
        return 1 if problems else 0
    finally:
        await sn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
