"""Timeline events: persisted to SQLite and broadcast to dashboard SSE subscribers."""
import asyncio
import json
import logging

from app import store

log = logging.getLogger("incident_flow")

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _broadcast(kind: str, payload: dict) -> None:
    data = json.dumps({"kind": kind, **payload}, default=str)
    for q in list(_subscribers):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


def emit(ticket: store.Ticket, stage: str, message: str, link: str | None = None) -> None:
    ev = store.add_event(store.Event(ticket_sys_id=ticket.sys_id, stage=stage, message=message, link=link))
    log.info("[%s] %s: %s", ticket.number, stage, message)
    _broadcast("event", {"ticket": ticket_dict(ticket), "event": ev.model_dump()})


def ticket_changed(ticket: store.Ticket) -> None:
    _broadcast("ticket", {"ticket": ticket_dict(ticket)})


def ticket_dict(t: store.Ticket) -> dict:
    return t.model_dump()
