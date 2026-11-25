"""FastAPI entrypoint: runs the agent loops and serves the live dashboard.

Run:  uv run uvicorn app.main:app --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import events, pilot_apps, poller, store
from app.chat import bot
from app.config import get_settings
from app.github_ops import GitHubOps
from app.servicenow import ServiceNow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    required = ["sn_username", "sn_password", "github_token", "github_repo"]
    missing = [k.upper() for k in required if not getattr(s, k)]
    if missing:
        raise RuntimeError(f"Missing settings in .env: {', '.join(missing)} (see .env.example)")
    store.engine()
    sn, gh = ServiceNow(s), GitHubOps(s)
    app.state.sn = sn
    tasks = [
        asyncio.create_task(poller.intake_loop(sn, gh, s)),
        asyncio.create_task(poller.watch_loop(sn, gh, s)),
    ]
    yield
    for t in tasks:
        t.cancel()
    await sn.close()


app = FastAPI(title="Incident Flow", lifespan=lifespan)


@app.get("/")
def dashboard():
    return FileResponse(DASHBOARD)


@app.get("/api/config")
def config():
    s = get_settings()
    return {"sn_instance_url": s.sn_instance_url, "github_repo": s.github_repo, "app_url": s.app_url,
            "model": s.claude_model, "apps": [a.public() for a in pilot_apps.all_apps()]}


@app.get("/api/tickets")
def tickets():
    return [{**t.model_dump(), "events": [e.model_dump() for e in store.events_for(t.sys_id)]} for t in store.all_tickets()]


@app.get("/events")
async def sse(request: Request):
    q = events.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"status": "ok", "tickets": len(store.all_tickets())}


# ---------- pilot chatbot ----------
class ChatMessage(BaseModel):
    text: str = ""
    confirm_draft: str | None = None


@app.post("/api/chat")
def chat_new():
    return bot.public_view(bot.new_chat())


@app.get("/api/chat/{chat_id}")
def chat_get(chat_id: str):
    chat = store.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    return bot.public_view(chat)


@app.post("/api/chat/{chat_id}/messages")
async def chat_send(chat_id: str, msg: ChatMessage, request: Request):
    if not msg.text.strip() and not msg.confirm_draft:
        raise HTTPException(400, "empty message")
    try:
        return await bot.respond(chat_id, msg.text, request.app.state.sn, msg.confirm_draft)
    except KeyError:
        raise HTTPException(404, "chat not found")
