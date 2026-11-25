"""SQLite persistence: tickets the agent has picked up, and their event timeline."""
from datetime import datetime, timezone

from sqlmodel import Field, Session, SQLModel, create_engine, select

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ticket(SQLModel, table=True):
    sys_id: str = Field(primary_key=True)
    table: str  # "incident" | "change_request"
    number: str
    short_description: str
    status: str = "new"  # new|triaging|diagnosing|pr_open|deploying|resolved|handed_off|failed
    scenario: str | None = None
    app: str | None = None  # pilot app id (pilot_apps.yaml)
    attempts: int | None = None  # follow-up fix PRs opened after failed validations
    pr_number: int | None = None
    pr_url: str | None = None
    merge_sha: str | None = None
    workflow: str | None = None
    run_url: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticket_sys_id: str = Field(index=True)
    stage: str  # intake|triage|diagnosis|pr|ci|merge|deploy|resolved|handoff|error
    message: str
    link: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ChatSession(SQLModel, table=True):
    """One pilot chatbot conversation. JSON text columns keep this dependency-free."""
    id: str = Field(primary_key=True)
    history: str = "[]"  # Claude API messages (text + tool blocks)
    transcript: str = "[]"  # what the chat UI shows
    state: str = "{}"  # turn counter, validated apps, pending draft, tickets raised
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


_engine = None


def _init(url: str):
    eng = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    _add_missing_columns(eng)
    return eng


def _add_missing_columns(eng) -> None:
    """create_all() never alters existing tables: add columns introduced after the DB was created."""
    from sqlalchemy import inspect, text
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(eng.dialect)}'))


def engine():
    global _engine
    if _engine is None:
        _engine = _init(get_settings().database_url)
    return _engine


def reset_engine(url: str) -> None:
    """Point the store at a different database (used by tests)."""
    global _engine
    _engine = _init(url)


def get_ticket(sys_id: str) -> Ticket | None:
    with Session(engine()) as s:
        return s.get(Ticket, sys_id)


def save_ticket(ticket: Ticket) -> Ticket:
    ticket.updated_at = utcnow()
    with Session(engine()) as s:
        merged = s.merge(ticket)
        s.commit()
        s.refresh(merged)
        return merged


def ticket_by_number(number: str) -> Ticket | None:
    with Session(engine()) as s:
        return s.exec(select(Ticket).where(Ticket.number == number.upper())).first()


def get_chat(chat_id: str) -> ChatSession | None:
    with Session(engine()) as s:
        return s.get(ChatSession, chat_id)


def save_chat(chat: ChatSession) -> ChatSession:
    chat.updated_at = utcnow()
    with Session(engine()) as s:
        merged = s.merge(chat)
        s.commit()
        s.refresh(merged)
        return merged


def tickets_in_status(*statuses: str) -> list[Ticket]:
    with Session(engine()) as s:
        return list(s.exec(select(Ticket).where(Ticket.status.in_(statuses))))


def all_tickets() -> list[Ticket]:
    with Session(engine()) as s:
        return list(s.exec(select(Ticket).order_by(Ticket.created_at.desc())))


def add_event(ev: Event) -> Event:
    with Session(engine()) as s:
        s.add(ev)
        s.commit()
        s.refresh(ev)
        return ev


def events_for(sys_id: str) -> list[Event]:
    with Session(engine()) as s:
        return list(s.exec(select(Event).where(Event.ticket_sys_id == sys_id).order_by(Event.id)))
