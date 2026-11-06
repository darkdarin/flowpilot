"""Long-term memory: facts the assistant knows about the home and about each person.

A fact is one short sentence ("Masha does not eat meat"). It belongs to the household
(``owner=None``: anyone in the home may hear it) or to one person (``owner`` = their user id, the
``Principal.user_id``): only that person's turns read it. The runtime keeps the facts; people see
and edit them in the editor; nodes get them through ``NodeContext.memory``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MAX_FACT_CHARS = 500


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    text: str
    owner: str | None  # a user id; None: the household's
    created_at: str  # ISO 8601, UTC
    source: str = "editor"  # who wrote it: "editor", or a node type such as "llm.agent"


class Memory(Protocol):
    """The runtime's facts, as a node sees them."""

    def facts(self, owner: str | None) -> list[Fact]:
        """The household's facts, plus ``owner``'s own when given; oldest first."""
        ...

    def add(self, text: str, owner: str | None, *, source: str) -> Fact: ...

    def remove(self, fact_id: str) -> bool: ...
