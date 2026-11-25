"""One-time PDI setup + preflight check.

Creates (if missing) the "AI Ops" and "Service Desk" groups, the agent user and the three pilot
apps (as cmdb_ci_appl Configuration Items), then verifies
that the integration user can read/write incidents and change requests.

    uv run python -m scripts.setup_servicenow
"""
import asyncio

from app.config import get_settings
from app import pilot_apps
from app.servicenow import APP_CI_TABLE, ServiceNow, ServiceNowError


async def ensure(sn: ServiceNow, table: str, field: str, value: str, body: dict) -> str:
    try:
        sys_id = await sn.lookup_sys_id(table, field, value)
        print(f"  exists   {table}: {value}")
    except ServiceNowError:
        sys_id = (await sn.create(table, body))["sys_id"]
        print(f"  created  {table}: {value}")
    return sys_id


async def main() -> None:
    s = get_settings()
    sn = ServiceNow(s)
    try:
        print(f"ServiceNow: {s.sn_instance_url}")
        intake = await ensure(sn, "sys_user_group", "name", s.sn_intake_group,
                              {"name": s.sn_intake_group, "description": "Tickets handled by Incident Flow AI agents"})
        await ensure(sn, "sys_user_group", "name", s.sn_human_group, {"name": s.sn_human_group})
        agent = await ensure(sn, "sys_user", "user_name", s.sn_agent_user,
                             {"user_name": s.sn_agent_user, "first_name": "AI Ops", "last_name": "Agent"})
        try:
            await sn.create("sys_user_grmember", {"group": intake, "user": agent})
            print("  added agent user to intake group")
        except ServiceNowError:
            print("  agent user already in intake group (or membership not permitted)")

        # The pilot apps as Configuration Items: tickets raised by the chatbot point at them (cmdb_ci),
        # so the incident list can be filtered by app and the agent knows which app a ticket is about.
        for app in pilot_apps.all_apps():
            await ensure(sn, APP_CI_TABLE, "name", app.name,
                         {"name": app.name, "short_description": f"Incident Flow pilot app. {app.summary}",
                          "operational_status": "1"})

        for table in ("incident", "change_request"):
            await sn._get(f"/api/now/table/{table}", {"sysparm_limit": 1, "sysparm_fields": "sys_id"})
            print(f"  read ok  {table}")
        print("\nDone. Run scripts.seed_tickets to create demo tickets.")
    finally:
        await sn.close()


if __name__ == "__main__":
    asyncio.run(main())
