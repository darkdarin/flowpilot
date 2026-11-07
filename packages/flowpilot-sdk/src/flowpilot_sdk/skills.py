"""Skills: tools people describe for the LLM agent on top of Home Assistant's actions.

A skill has a name and a description for the model, fields the model fills in (text, number, a
choice from a list), and actions: Home Assistant actions (``androidtv.adb_command``,
``media_player.select_source``, ``script.turn_on``...) whose data uses the fields. The model only
fills the fields; what runs is what the person wrote. Text goes into an action only escaped
(``{query|url}``, ``{query|shell}``), so a phrase cannot become a command. Nodes get skills through
``NodeContext.skills``; ``render`` turns a call into the actions to run.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import quote

TOOL_PREFIX = "skill__"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)(?:\|([a-z]+))?\}")
FILTERS = ("url", "shell")
MAX_TEXT = 300

FieldType = Literal["text", "number", "choice"]


@dataclass(frozen=True, slots=True)
class SkillField:
    name: str
    title: str = ""  # what it is, for the model
    type: FieldType = "text"
    options: tuple[str, ...] = ()  # for "choice": the allowed values
    required: bool = True


@dataclass(frozen=True, slots=True)
class SkillAction:
    action: str  # a Home Assistant action, "domain.service"
    data: Mapping[str, Any] = field(default_factory=dict)  # may use {field}, {field|url}
    target: Mapping[str, Any] = field(default_factory=dict)  # entity_id, area_id, ...


@dataclass(frozen=True, slots=True)
class Skill:
    id: str
    name: str  # the tool is "skill__<name>"
    description: str  # when and why to use it, for the model
    fields: tuple[SkillField, ...]
    actions: tuple[SkillAction, ...]
    enabled: bool = True

    @property
    def tool(self) -> str:
        return TOOL_PREFIX + self.name


class Skills(Protocol):
    """The runtime's skills, as a node sees them."""

    def all(self) -> list[Skill]: ...


def check(skill: Skill) -> None:
    """``ValueError`` when a skill cannot be offered: bad names, actions, or a placeholder
    without a field, or text put in unescaped."""
    if not NAME_RE.match(skill.name):
        raise ValueError("a skill name is latin letters, digits and _, starting with a letter")
    if not skill.description.strip():
        raise ValueError("describe when the assistant should use the skill")
    names = [f.name for f in skill.fields]
    if len(set(names)) != len(names) or not all(NAME_RE.match(n) for n in names):
        raise ValueError("field names must be unique latin names")
    kinds = {f.name: f for f in skill.fields}
    for f in skill.fields:
        if f.type == "choice" and not f.options:
            raise ValueError(f"the choice {f.name!r} needs options")
    if not skill.actions:
        raise ValueError("a skill does at least one action")
    for action in skill.actions:
        if not SERVICE_RE.match(action.action):
            raise ValueError(f"{action.action!r} is not a Home Assistant action (domain.service)")
        for text in _strings(action.data) + _strings(action.target):
            for name, filt in PLACEHOLDER_RE.findall(text):
                if name not in kinds:
                    raise ValueError(f"{{{name}}} is not a field of the skill")
                if filt and filt not in FILTERS:
                    raise ValueError(f"unknown filter |{filt} (use |url or |shell)")
                if kinds[name].type == "text" and not filt and text != f"{{{name}}}":
                    raise ValueError(
                        f"text {{{name}}} inside a command must be escaped: "
                        f"{{{name}|url}} or {{{name}|shell}}"
                    )


def definition(skill: Skill) -> dict[str, Any]:
    """The skill as an OpenAI-style function tool."""
    properties: dict[str, Any] = {}
    for f in skill.fields:
        prop: dict[str, Any] = {"description": f.title} if f.title else {}
        if f.type == "number":
            prop["type"] = "number"
        elif f.type == "choice":
            prop |= {"type": "string", "enum": list(f.options)}
        else:
            prop |= {"type": "string", "maxLength": MAX_TEXT}
        properties[f.name] = prop
    return {
        "type": "function",
        "function": {
            "name": skill.tool,
            "description": skill.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [f.name for f in skill.fields if f.required],
            },
        },
    }


def render(skill: Skill, args: Mapping[str, Any]) -> list[SkillAction]:
    """The actions to run for a call, with the fields filled in; ``ValueError`` for bad values."""
    values: dict[str, Any] = {}
    for f in skill.fields:
        value = args.get(f.name)
        if value is None or value == "":
            if f.required:
                raise ValueError(f"{f.name} is required")
            value = ""
        elif f.type == "number":
            try:
                value = float(value)
            except TypeError, ValueError:
                raise ValueError(f"{f.name} must be a number") from None
            value = int(value) if value.is_integer() else value
        elif f.type == "choice":
            if str(value) not in f.options:
                raise ValueError(f"{f.name} must be one of: {', '.join(f.options)}")
            value = str(value)
        else:
            value = " ".join(str(value).split())[:MAX_TEXT]
        values[f.name] = value
    return [
        SkillAction(a.action, _fill(a.data, values), _fill(a.target, values)) for a in skill.actions
    ]


def _fill(value: Any, values: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER_RE.fullmatch(value)
    if whole and not whole.group(2):
        return values[whole.group(1)]  # "{volume}" keeps its number

    def one(match: re.Match[str]) -> str:
        text = str(values[match.group(1)])
        if match.group(2) == "url":
            return quote(text, safe="")
        if match.group(2) == "shell":
            return shlex.quote(text)
        return text

    return PLACEHOLDER_RE.sub(one, value)


def _strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []
