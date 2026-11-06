"""The runtime's long-term memory: facts about the home and its people (``flowpilot_sdk.Memory``).

Facts live in one JSON file next to the runtime's config (``memory_file``, ``memory.json`` by
default), readable by the runtime's user only, and go into Home Assistant backups with the rest of
the app's data. The editor lists, adds, edits and removes them; nodes (the LLM agent) read them
for every turn and may add or remove them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flowpilot_sdk import MAX_FACT_CHARS, Fact, new_ulid

MAX_FACTS = 1000


class MemoryStore:
    """Facts written through to ``path`` (mode 600) on every change; in memory when ``None``."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._facts: dict[str, Fact] = {}
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            for item in data.get("facts", []) if isinstance(data, dict) else []:
                try:
                    fact = Fact(
                        id=str(item["id"]),
                        text=str(item["text"]),
                        owner=None if item.get("owner") is None else str(item["owner"]),
                        created_at=str(item["created_at"]),
                        source=str(item.get("source") or "editor"),
                    )
                except KeyError, TypeError:
                    continue
                self._facts[fact.id] = fact

    def all(self) -> list[Fact]:
        return list(self._facts.values())

    def facts(self, owner: str | None) -> list[Fact]:
        return [f for f in self._facts.values() if f.owner is None or f.owner == owner]

    def add(self, text: str, owner: str | None, *, source: str) -> Fact:
        fact = Fact(
            id=new_ulid(),
            text=_clean(text),
            owner=owner or None,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source=source,
        )
        if len(self._facts) >= MAX_FACTS:
            raise ValueError(f"the memory is full ({MAX_FACTS} facts): remove some first")
        self._facts[fact.id] = fact
        self._save()
        return fact

    def update(self, fact_id: str, *, text: str, owner: str | None) -> Fact:
        fact = replace(self._facts[fact_id], text=_clean(text), owner=owner or None)
        self._facts[fact_id] = fact
        self._save()
        return fact

    def remove(self, fact_id: str) -> bool:
        if self._facts.pop(fact_id, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        data: dict[str, Any] = {"version": 1, "facts": [asdict(f) for f in self._facts.values()]}
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        tmp.replace(self.path)


def _clean(text: str) -> str:
    text = " ".join(str(text).split())
    if not text or len(text) > MAX_FACT_CHARS:
        raise ValueError(f"a fact must be 1 to {MAX_FACT_CHARS} characters")
    return text
