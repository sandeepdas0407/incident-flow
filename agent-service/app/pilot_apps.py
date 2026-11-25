"""The pilot app registry (pilot_apps.yaml): the only apps tickets may be raised for and changed."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings

REGISTRY_FILE = Path(__file__).resolve().parents[1] / "pilot_apps.yaml"


@dataclass(frozen=True)
class PilotApp:
    id: str
    name: str
    path: str  # folder in the demo repo, e.g. "apps/inventory"
    port: int
    summary: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        base = get_settings().app_url.rstrip("/")
        return base if self.port == 80 or not base else f"{base}:{self.port}"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "summary": self.summary, "url": self.url}


@dataclass
class Match:
    status: str  # "match" | "suggestions" | "not_in_pilot"
    app: PilotApp | None = None
    candidates: list[PilotApp] = field(default_factory=list)


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    text = re.sub(r"\b(the|app|application|system|portal|site|website|service)\b", " ", text)
    return " ".join(text.split())


@lru_cache
def load(path: str | None = None) -> tuple[PilotApp, ...]:
    data = yaml.safe_load(Path(path or REGISTRY_FILE).read_text(encoding="utf-8"))
    return tuple(PilotApp(id=a["id"], name=a["name"], path=a["path"].rstrip("/"), port=int(a["port"]),
                          summary=a["summary"], aliases=tuple(a.get("aliases", [])))
                 for a in data["apps"])


def all_apps() -> tuple[PilotApp, ...]:
    return load()


def get(app_id: str | None) -> PilotApp | None:
    return next((a for a in load() if a.id == app_id), None)


def by_name(name: str | None) -> PilotApp | None:
    """Exact (case/punctuation-insensitive) match on the display name or an alias."""
    if not name:
        return None
    key = _norm(name)
    for a in load():
        if key and key in {_norm(a.name), _norm(a.id), *(_norm(x) for x in a.aliases)}:
            return a
    return None


def match(name: str) -> Match:
    """Validate a user-supplied app name. Only an exact name/alias match counts as a match;
    near misses (typos) come back as suggestions that the user must confirm."""
    exact = by_name(name)
    if exact:
        return Match("match", app=exact)
    key = _norm(name)
    if not key:
        return Match("not_in_pilot")
    keys = {}
    for a in load():
        for k in (a.name, a.id, *a.aliases):
            keys.setdefault(_norm(k), a)
    close = difflib.get_close_matches(key, list(keys), n=3, cutoff=0.75)
    # a vague name ("contoso") that is part of several app names: offer them all
    words = set(key.split())
    partial = [a for a in load() if words <= set(_norm(a.name).split())]
    candidates = list(dict.fromkeys([*(keys[c] for c in close), *partial]))
    return Match("suggestions", candidates=candidates) if candidates else Match("not_in_pilot")
