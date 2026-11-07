"""Tools the agent may call; FlowPilot executes them, with the speaker's rights (ADR-018).

A ``Toolbox`` offers OpenAI-style function definitions and runs a call to a short text result for
the model. The first tools are the long-term memory's (ADR-019): ``remember`` and ``forget``.
Rights are checked here, in the executor, not in the prompt: a person's own facts need a known
speaker, and nobody can see or forget another person's facts.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flowpilot_sdk import MAX_FACT_CHARS, Memory


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: Mapping[str, Any]
    run: Callable[[Mapping[str, Any]], Awaitable[str]]

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class Toolbox:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def __bool__(self) -> bool:
        return bool(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    async def call(self, name: str, arguments: str) -> tuple[str, bool]:
        """The result for the model and whether the call succeeded; never raises for bad calls."""
        tool = self._tools.get(name)
        if tool is None:
            return f"error: there is no tool {name!r}", False
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError as err:
            return f"error: bad arguments: {err}", False
        try:
            return await tool.run(args), True
        except ValueError as err:
            return f"error: {err}", False


def memory_tools(
    memory: Memory, speaker: str | None, *, personal: bool, auto: bool = False
) -> list[Tool]:
    """``remember`` and ``forget`` for one turn: ``speaker`` is who speaks (None: unknown);
    ``auto``: the agent also keeps what it learns without being asked."""
    own = speaker if personal else None

    async def remember(args: Mapping[str, Any]) -> str:
        text = str(args.get("fact") or "").strip()
        about = args.get("about", "home")
        if not text:
            raise ValueError("the fact is empty")
        if about == "speaker":
            if own is None:
                raise ValueError(
                    "it is not known who is speaking, so a personal fact cannot be kept; "
                    "say so, or keep it as the home's if it is about the home"
                )
            owner = own
        else:
            owner = None
        same = " ".join(text.lower().split())
        if any(" ".join(f.text.lower().split()) == same for f in memory.facts(owner)):
            return "already remembered"
        memory.add(text, owner, source="llm.agent")
        return "remembered about the speaker" if owner else "remembered for the home"

    async def forget(args: Mapping[str, Any]) -> str:
        wanted = " ".join(str(args.get("fact") or "").lower().split())
        if not wanted:
            raise ValueError("say which fact to forget")
        visible = [f for f in memory.facts(own) if f.owner is None or f.owner == own]
        exact = [f for f in visible if f.text.lower() == wanted]
        found = exact or [f for f in visible if wanted in f.text.lower()]
        if not found:
            return "no such fact is remembered"
        if len(found) > 1:
            listed = "; ".join(f.text for f in found[:5])
            return f"several facts match, be more precise: {listed}"
        memory.remove(found[0].id)
        return f"forgotten: {found[0].text}"

    when = (
        "when the user asks to remember something, or tells a lasting fact about the home or "
        "the family worth knowing later (names, pets, routines, habits, preferences); never "
        "for sensitive or passing things"
        if auto
        else "when the user asks to remember something"
    )
    return [
        Tool(
            "remember",
            f"Keep a fact in long-term memory. Use it {when}. One short sentence in the third "
            "person, e.g. 'Masha does not eat meat'.",
            {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "maxLength": MAX_FACT_CHARS},
                    "about": {
                        "type": "string",
                        "enum": ["speaker", "home"],
                        "description": "'home': for everyone in the home: the family, pets, "
                        "guests, the house and its routines; 'speaker': the speaker's own "
                        "preferences and habits, private to them.",
                    },
                },
                "required": ["fact", "about"],
            },
            remember,
        ),
        Tool(
            "forget",
            "Remove a remembered fact when the user asks to forget it or it is no longer true. "
            "Give the fact's text as you know it (or a distinctive part of it).",
            {
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            forget,
        ),
    ]
