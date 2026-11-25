"""Create demo tickets in ServiceNow, routed to the AI Ops group (the chatbot is the other way in).

    uv run python -m scripts.seed_tickets                  # the default set: vm, bug, color, handoff
    uv run python -m scripts.seed_tickets inv-bug book-color
    Scenarios: vm, bug, color, handoff, inv-bug, book-bug, inv-color, book-color, outside-pilot
"""
import asyncio
import sys

from app.config import get_settings
from app.servicenow import ServiceNow

ORDERS_TRACE = """Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/flask/app.py", line 917, in full_dispatch_request
    rv = self.dispatch_request()
  File "/opt/app/apps/orders/app.py", line 35, in api_orders
    return jsonify(orders=ORDERS, stats=compute_order_stats(ORDERS))
  File "/opt/app/apps/orders/app.py", line 19, in compute_order_stats
    avg_shipped = sum(o["amount"] for o in shipped) / len(shipped)
ZeroDivisionError: division by zero"""

INVENTORY_TRACE = """Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/flask/app.py", line 917, in full_dispatch_request
    rv = self.dispatch_request()
  File "/opt/app/apps/inventory/app.py", line 33, in api_stock
    return jsonify(items=ITEMS, low_stock=low_stock(ITEMS))
  File "/opt/app/apps/inventory/app.py", line 18, in low_stock
    return [i["sku"] for i in items if i["qty"] < i["reorder_level"]]
KeyError: 'reorder_level'"""

BOOKINGS_TRACE = """Traceback (most recent call last):
  File "/opt/venv/lib/python3.12/site-packages/flask/app.py", line 917, in full_dispatch_request
    rv = self.dispatch_request()
  File "/opt/app/apps/bookings/app.py", line 39, in api_bookings
    return jsonify(bookings=with_weekday(BOOKINGS))
  File "/opt/app/apps/bookings/app.py", line 22, in with_weekday
    day = datetime.strptime(b["date"], "%Y-%m-%d")
ValueError: time data '04/12/2026' does not match format '%Y-%m-%d'"""

# name -> (table, pilot app name or None, fields)
SCENARIOS = {
    "vm": ("incident", "Contoso Orders", {
        "short_description": "High CPU and memory on incidentflow-vm (pilot apps slow)",
        "description": ("Azure Monitor alert: incidentflow-vm (Standard_B1s) CPU > 95% for 20 minutes and available "
                        "memory < 5%. Page load times for the pilot apps are above 8s. Traffic has doubled since the "
                        "marketing campaign started; no recent deployments."),
        "urgency": "2", "impact": "2", "category": "hardware",
    }),
    "bug": ("incident", "Contoso Orders", {
        "short_description": "Contoso Orders /api/orders returning HTTP 500",
        "description": f"All calls to GET /api/orders fail with 500 Internal Server Error since this morning.\n\nApp log:\n{ORDERS_TRACE}",
        "urgency": "1", "impact": "2", "category": "software",
    }),
    "color": ("change_request", "Contoso Orders", {
        "short_description": "Change Contoso Orders background colour to brand blue (#0B5FFF)",
        "description": "Marketing requests the Contoso Orders app background be changed to the new brand blue #0B5FFF.",
        "risk": "4", "impact": "3",
    }),
    "handoff": ("incident", None, {
        "short_description": "Cannot log in to my laptop after password change",
        "description": "User reports being locked out of their Windows laptop after changing their AD password.",
        "urgency": "3", "impact": "3", "category": "inquiry",
    }),
    "inv-bug": ("incident", "Contoso Inventory", {
        "short_description": "Contoso Inventory /api/stock returning HTTP 500",
        "description": ("The stock dashboard is blank: GET /api/stock fails with 500 since the new supplier feed "
                        f"was loaded this morning.\n\nApp log:\n{INVENTORY_TRACE}"),
        "urgency": "1", "impact": "2", "category": "software",
    }),
    "book-bug": ("incident", "Contoso Bookings", {
        "short_description": "Contoso Bookings /api/bookings returning HTTP 500",
        "description": ("The room calendar won't load: GET /api/bookings fails with 500 after bookings were imported "
                        f"from the legacy system.\n\nApp log:\n{BOOKINGS_TRACE}"),
        "urgency": "2", "impact": "2", "category": "software",
    }),
    "inv-color": ("change_request", "Contoso Inventory", {
        "short_description": "Change Contoso Inventory background colour to warehouse amber (#FFB020)",
        "description": "Operations asks for the Contoso Inventory background to be amber #FFB020 to match the warehouse signage.",
        "risk": "4", "impact": "3",
    }),
    "book-color": ("change_request", "Contoso Bookings", {
        "short_description": "Change Contoso Bookings background colour to teal (#0E9384)",
        "description": "Facilities requests the Contoso Bookings background be changed to teal #0E9384.",
        "risk": "4", "impact": "3",
    }),
    "outside-pilot": ("change_request", None, {
        "short_description": "Change Payroll portal background colour to green (#2E7D32)",
        "description": "HR asks for the Payroll portal background to be green #2E7D32. (Payroll is not a pilot app.)",
        "risk": "4", "impact": "3",
    }),
}
DEFAULT = ["vm", "bug", "color", "handoff"]


async def main(names: list[str]) -> None:
    s = get_settings()
    sn = ServiceNow(s)
    try:
        group = await sn.group_id(s.sn_intake_group)
        for name in names or DEFAULT:
            table, app_name, body = SCENARIOS[name]
            body = {**body, "assignment_group": group}
            if app_name:
                body["cmdb_ci"] = await sn.app_ci_id(app_name)
            if table == "change_request":
                body.update(await sn.standard_change_fields())
            rec = await sn.create(table, body)
            print(f"created {table} {rec['number']}  ({name}{', ' + app_name if app_name else ''})")
    finally:
        await sn.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
