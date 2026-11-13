"""Tools the agent may call; FlowPilot executes them, with the speaker's rights (ADR-018).

A ``Toolbox`` offers OpenAI-style function definitions and runs a call to a short text result for
the model. The first tools are the long-term memory's (ADR-019): ``remember`` and ``forget``.
Rights are checked here, in the executor, not in the prompt: a person's own facts need a known
speaker, and nobody can see or forget another person's facts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from flowpilot_sdk import (
    MAX_FACT_CHARS,
    Action,
    HostError,
    HostUnavailable,
    Memory,
    QuickCommand,
    QuickCommands,
    Skill,
)
from flowpilot_sdk.commands import MAX_PHRASE_CHARS
from flowpilot_sdk.skills import render


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

    def with_tools(self, tools: list[Tool]) -> Toolbox:
        return Toolbox([*self._tools.values(), *tools])

    def __len__(self) -> int:
        return len(self._tools)

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


def command_tools(commands: QuickCommands, speaker: str | None, home: set[str]) -> list[Tool]:
    """``save_quick_command`` and ``forget_quick_command``: quick commands people teach.

    ``home`` are the Home Assistant tools of this turn: a command may only replay those.
    """

    def norm(phrase: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", phrase.casefold().replace("ё", "е")).split())

    def known() -> None:
        if speaker is None:
            raise ValueError("it is not known who is speaking: only a known person may teach")

    def phrase_list(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            raise ValueError("phrases must be a list of strings")
        return [str(p) for p in value]

    def action_list(value: Any) -> list[Action] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("actions must be a list")
        actions = []
        for item in value:
            if not isinstance(item, Mapping) or str(item.get("tool")) not in home:
                raise ValueError(
                    f"{item!r}: an action must be one of the Home Assistant tools you have"
                )
            arguments = item.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            actions.append(Action(str(item["tool"]), dict(arguments)))
        return actions

    def having(phrases: list[str]) -> list[QuickCommand]:
        """The commands that already have any of these phrases."""
        wanted = {norm(p) for p in phrases}
        return [c for c in commands.all() if wanted & {norm(p) for p in c.phrases}]

    def find(phrase: str) -> QuickCommand:
        wanted = norm(phrase)
        if not wanted:
            raise ValueError("say which command")
        exact = [c for c in commands.all() if wanted in {norm(p) for p in c.phrases}]
        found = exact or [c for c in commands.all() if any(wanted in norm(p) for p in c.phrases)]
        if not found:
            raise ValueError("no such quick command")
        if len(found) > 1:
            listed = "; ".join(c.phrases[0] for c in found[:5])
            raise ValueError(f"several commands match, be more precise: {listed}")
        return found[0]

    async def save(args: Mapping[str, Any]) -> str:
        known()
        phrases = phrase_list(args.get("phrases"))
        actions = action_list(args.get("actions"))
        if not phrases or not actions:
            raise ValueError("give phrases (a list) and actions (a list)")
        reply = str(args.get("reply") or "")
        same = having(phrases)
        if len(same) > 1:
            listed = "; ".join(c.phrases[0] for c in same)
            raise ValueError(f"these phrases belong to several commands: {listed}")
        if same:  # saving it again changes it, never makes a copy
            command = commands.update(same[0].id, phrases=phrases, reply=reply, actions=actions)
            return f"updated: «{'», «'.join(command.phrases)}»"
        command = commands.add(phrases, actions, reply, created_by=speaker, source="llm.agent")
        return f"saved: «{'», «'.join(command.phrases)}» now runs at once, without you"

    async def update(args: Mapping[str, Any]) -> str:
        known()
        command = find(str(args.get("phrase") or ""))
        phrases = list(command.phrases)
        phrases += phrase_list(args.get("add_phrases")) or []
        drop = {norm(p) for p in phrase_list(args.get("remove_phrases")) or []}
        phrases = [p for p in phrases if norm(p) not in drop]
        reply = command.reply if args.get("reply") is None else str(args.get("reply"))
        changed = commands.update(
            command.id, phrases=phrases, reply=reply, actions=action_list(args.get("actions"))
        )
        return (
            f"updated: «{'», «'.join(changed.phrases)}»; "
            f"{len(changed.actions)} actions; reply «{changed.reply}»"
        )

    async def forget(args: Mapping[str, Any]) -> str:
        if speaker is None:
            raise ValueError("it is not known who is speaking: only a known person may change it")
        wanted = " ".join(str(args.get("phrase") or "").casefold().split())
        if not wanted:
            raise ValueError("say which command to forget")
        found = [
            c
            for c in commands.all()
            if any(wanted == p.casefold() or wanted in p.casefold() for p in c.phrases)
        ]
        if not found:
            return "no such quick command"
        if len(found) > 1:
            listed = "; ".join(c.phrases[0] for c in found[:5])
            return f"several commands match, be more precise: {listed}"
        commands.remove(found[0].id)
        return f"forgotten: «{found[0].phrases[0]}»"

    action = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "A Home Assistant tool you called."},
            "arguments": {"type": "object", "description": "Its arguments, as you called it."},
        },
        "required": ["tool", "arguments"],
    }
    return [
        Tool(
            "save_quick_command",
            "Teach the home a quick command: after this, saying one of the phrases does the "
            "actions at once, without you. Only after the person agreed. Actions are the Home "
            "Assistant tool calls that do it (several for a scene-like command), exactly as you "
            "called them; prefer names and areas to 'here'.",
            {
                "type": "object",
                "properties": {
                    "phrases": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": MAX_PHRASE_CHARS},
                        "description": "How people say it, e.g. 'режим кино', 'включи кино'.",
                    },
                    "actions": {"type": "array", "items": action},
                    "reply": {
                        "type": "string",
                        "description": "A short answer when done, e.g. 'Приятного просмотра'.",
                    },
                },
                "required": ["phrases", "actions"],
            },
            save,
        ),
        Tool(
            "update_quick_command",
            "Change a quick command the home was taught: add or remove phrases, replace its "
            "actions or its reply. Never save a changed command again as a new one.",
            {
                "type": "object",
                "properties": {
                    "phrase": {
                        "type": "string",
                        "description": "One of its phrases now, to find it.",
                    },
                    "add_phrases": {"type": "array", "items": {"type": "string"}},
                    "remove_phrases": {"type": "array", "items": {"type": "string"}},
                    "actions": {
                        "type": "array",
                        "items": action,
                        "description": "All its actions, replacing the old ones (only if asked).",
                    },
                    "reply": {"type": "string"},
                },
                "required": ["phrase"],
            },
            update,
        ),
        Tool(
            "forget_quick_command",
            "Remove a quick command the home was taught, when the person asks.",
            {
                "type": "object",
                "properties": {"phrase": {"type": "string"}},
                "required": ["phrase"],
            },
            forget,
        ),
    ]


CONFIRM_TOOL = "confirm_with_sound"


def confirm_tool(mode: str) -> Tool:
    """``confirm_with_sound``: the agent asks to confirm the actions of the same reply with the
    satellite's sound instead of words. The agent decides it after the actions ran (see
    ``LlmAgent._confirmation``); this tool only describes it to the model."""

    async def run(args: Mapping[str, Any]) -> str:
        raise ValueError("call it together with the actions it confirms")

    where = (
        " It is played only when the devices are in the room of the speaker that heard the "
        "request; otherwise you are asked to answer in words."
        if mode == "area"
        else ""
    )
    return Tool(
        CONFIRM_TOOL,
        "Confirm a simple device command with a short sound instead of words, and end your "
        "answer. It does nothing by itself: call it only in the same reply as the Home Assistant "
        "actions that fully do what was asked (turn on or off, open, close, set), when nothing "
        "needs saying: no question, nothing unclear, nothing to tell. Not for music, searches or "
        "questions; never alone, never instead of the action, never before the last action. If "
        "an action fails there is no sound and you answer in words." + where,
        {"type": "object", "properties": {}},
        run,
    )


SERVICE_CALL_OP = "ha.service.call"


async def run_skill(
    turn: Any, skill: Skill, args: Mapping[str, Any], who: Mapping[str, Any]
) -> list[Any]:
    """Run a skill's actions in order with the fields filled in; what Home Assistant answered
    for each. ``ValueError`` (shown to the model) for bad values or a failed action."""
    results: list[Any] = []
    for action in render(skill, args):
        call = {
            **who,
            "action": action.action,
            "data": dict(action.data),
            "target": dict(action.target),
        }
        try:
            answer = await turn.call_host(SERVICE_CALL_OP, call)
        except (HostError, HostUnavailable) as err:
            raise ValueError(f"Home Assistant did not run {action.action}: {err}") from err
        data = answer.get("data") if isinstance(answer, Mapping) else None
        if not isinstance(answer, Mapping) or answer.get("error"):
            message = data.get("message") if isinstance(data, Mapping) else None
            raise ValueError(f"{action.action} failed: {message or answer}")
        results.append(data)
    return results
