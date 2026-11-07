"""Learned quick commands: a phrase people say and what it does in the home.

The LLM agent saves one (after the person agreed) when it did something with Home Assistant's
tools that people are likely to ask for again; the editor lists them. A command may do several
things ("movie night": dim the lights, turn on the TV, set its volume): its actions are the tool
calls the agent made, replayed in order by Quick commands without an LLM, with the speaking
person's rights. Nodes get them through ``NodeContext.commands``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_PHRASES = 10
MAX_ACTIONS = 10
MAX_PHRASE_CHARS = 200
# tools that are not about the home: a learned command never replays them
NOT_REPLAYED = frozenset({"remember", "forget", "save_quick_command", "forget_quick_command"})


@dataclass(frozen=True, slots=True)
class Action:
    tool: str  # a Home Assistant LLM tool, e.g. "intent__HassTurnOn"
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuickCommand:
    id: str
    phrases: tuple[str, ...]  # what people say, as they say it
    actions: tuple[Action, ...]
    reply: str  # what to answer when done; empty: a plain "done"
    created_at: str  # ISO 8601, UTC
    created_by: str | None = None  # the user id of who taught it
    source: str = "editor"  # "editor", or a node type such as "llm.agent"
    uses: int = 0


class QuickCommands(Protocol):
    """The runtime's learned commands, as a node sees them."""

    def all(self) -> list[QuickCommand]: ...

    def add(
        self,
        phrases: list[str],
        actions: list[Action],
        reply: str,
        *,
        created_by: str | None,
        source: str,
    ) -> QuickCommand: ...

    def remove(self, command_id: str) -> bool: ...

    def used(self, command_id: str) -> None:
        """Count one more use (the editor shows which commands people rely on)."""
        ...
