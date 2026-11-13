"""``llm.agent``: a lightweight agent on any OpenAI-compatible chat API (ADR-018).

The answer is streamed (``stream: true``): each piece goes out on ``delta`` as it comes, so
text-to-speech starts on the first words. The dialogue's history (the user's utterances and the
answers) is kept in the session's state, per node, for the configured number of exchanges and
hours (the runtime saves it across restarts), so a follow-up question in the same conversation has
its context. The system prompt may use
``{date}``, ``{time}``, ``{weekday}`` and ``{language}``.

One HTTP session is kept for the node's life, so later turns reuse the connection (and its TLS
handshake) to the provider.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    DYNAMIC_KEY,
    ENUM_TITLES_KEY,
    GROUP_KEY,
    GROUPS_KEY,
    LOG_EVENT,
    NODE_OPTIONS_KEY,
    ConfigOption,
    HostError,
    HostUnavailable,
    Memory,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    QuickCommands,
    Response,
    Skill,
    Skills,
    TextDelta,
    TurnSession,
    Utterance,
    dialogue,
    node_translations,
)
from flowpilot_sdk.language import LANGUAGE_META, language_by_script
from flowpilot_sdk.skills import definition as skill_definition

from .tools import (
    CONFIRM_TOOL,
    Tool,
    Toolbox,
    command_tools,
    confirm_tool,
    memory_tools,
    run_skill,
)

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_HISTORY_HOURS = 24
# the Home Assistant calls of the last answer, kept for "yes, remember it" in the next turn
LAST_CALLS_KEY = "home_calls"
LAST_CALLS_S = 600.0
# Who lives in the home, from the turn's client (Home Assistant: people and users)
PEOPLE_OP = "ha.people.list"
PEOPLE_TIMEOUT_S = 2.0
# Home Assistant's Assist LLM API: instructions and tools for the entities exposed to Assist
HOME_TOOLS_OP = "ha.llm.tools"
HOME_CALL_OP = "ha.llm.call"
HOME_TOOLS_TIMEOUT_S = 3.0
HOME_CALL_TIMEOUT_S = 20.0
# a Home Assistant action of a skill the owner described (flowpilot_sdk.skills)
SERVICE_CALL_OP = "ha.service.call"
# Rounds of tool calls in one turn; the last round must answer in words.
MAX_TOOL_ROUNDS = 4
# Answer language by its text: the first words wait until this many letters, or a sentence ended
LANGUAGE_LETTERS = 16


def _language_clear(text: str) -> bool:
    """Enough of the answer to tell its alphabet."""
    letters = sum(c.isalpha() for c in text)
    return letters >= LANGUAGE_LETTERS or (letters > 0 and any(c in ".!?…\n" for c in text))


# the state of the home when the turn began, from Home Assistant's live context
STATE_TEXT = (
    "The state of the home as this request began (you need not ask for it before acting; ask "
    "again only to see what changed after your own actions):\n{}"
)
# Home Assistant intents and the sound that confirms them (``acknowledge`` on the ack answer)
ACK_KINDS = {"HassTurnOn": "on", "HassTurnOff": "off"}
DONE = {"ru": "Готово", "en": "Done"}


DETAIL_CHARS = 8000  # one journal entry's content in live debug


class Journal:
    """The agent's journal for live debug (``LOG_EVENT``): what went into the model, what came
    back, which tools ran with what. Written only while someone watches: it holds what people
    said, the prompt and the facts."""

    def __init__(self, turn: TurnSession) -> None:
        self._turn = turn
        self.on = bool(getattr(turn, "watched", False))

    def __call__(self, kind: str, summary: str, detail: str | None = None, **attrs: Any) -> None:
        if not self.on:
            return
        extra = {"detail": detail[:DETAIL_CHARS]} if detail else {}
        self._turn.trace(LOG_EVENT, kind=kind, summary=summary, **extra, **attrs)


def _parsed(text: str) -> Any:
    """JSON text as data (the editor shows it as a tree), anything else as it is."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _tool_detail(arguments: str, result: str) -> str:
    """A tool call for the journal: its arguments and result as one JSON object."""
    return json.dumps(
        {"arguments": _parsed(arguments or "{}"), "result": _parsed(result)},
        ensure_ascii=False,
        indent=2,
    )


class LlmError(Exception):
    """The provider rejected the request or the answer broke off."""


def _key(config: Mapping[str, Any]) -> str | None:
    """The API key of a config whose secret reference the runtime resolved (setting choices)."""
    key = config.get("api_key")
    return key if isinstance(key, str) and key else None


def _headers(key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _base(config: Mapping[str, Any]) -> str:
    return str(config.get("base_url") or DEFAULT_URL).rstrip("/")


async def _error_text(resp: aiohttp.ClientResponse) -> str:
    body = (await resp.text())[:400]
    try:
        data = json.loads(body)
        message = data.get("error", {}).get("message") if isinstance(data, dict) else None
    except ValueError, AttributeError:
        message = None
    return f"HTTP {resp.status}: {message or body}"


def _memory_text(home: list[str], own: list[str]) -> str:
    if not home and not own:
        return ""
    parts = ["What you know from long-term memory (use it when it helps; do not recite it):"]
    if home:
        parts += ["About the home:", *(f"- {t}" for t in home)]
    if own:
        parts += ["About the person you are talking to:", *(f"- {t}" for t in own)]
    return "\n".join(parts)


def _people_text(people: list[Mapping[str, Any]] | None, speaker: str | None) -> str:
    """Who is talking and who lives in the home, with where they are."""
    if not people:
        return ""
    lines = ["People of the home:"]
    talking = None
    for person in people:
        name = str(person.get("name") or "").strip()
        if not name:
            continue
        state = person.get("state")
        where = {"home": "at home", "not_home": "away", None: "", "unknown": ""}.get(
            state, f"at {state}"
        )
        lines.append(f"- {name}" + (f" ({where})" if where else ""))
        if speaker is not None and person.get("user_id") == speaker:
            talking = name
    if len(lines) == 1:
        return ""
    who = (
        f"You are talking to {talking}."
        if talking
        else "It is not known which of them you are talking to."
    )
    return "\n".join([*lines, who])


TEACH_TEXT = (
    "You can teach the home quick commands (save_quick_command): Home Assistant's quick "
    "commands then do them at once, without you, when someone says the phrase. After you did "
    "something with Home Assistant's tools that people are likely to ask for again in the same "
    "words, above all several things at once (a scene such as 'movie night') or a device called "
    "by a nickname, offer in one short sentence to remember it as a quick command. Save only "
    "after the person agrees, with the phrases they use and exactly the tool calls you made. Do "
    "not offer for questions or one-off requests, and not again for what was declined. To change "
    "a quick command (more phrases, other actions, another reply) use update_quick_command, "
    "never save it again as a new one; to forget one, forget_quick_command."
)


def acknowledge_kind(tools: list[str]) -> str:
    """What the actions did, for the sound: ``on`` when all turned something on, ``off`` when
    all turned something off, else ``done``."""
    kinds = {ACK_KINDS.get(name.removeprefix("intent__"), "done") for name in tools}
    return kinds.pop() if len(kinds) == 1 else "done"


def _speech_of(data: Any) -> str:
    """The words of a Home Assistant intent's answer (``speech.plain.speech``), or nothing."""
    speech = data.get("speech") if isinstance(data, Mapping) else None
    plain = speech.get("plain") if isinstance(speech, Mapping) else None
    text = plain.get("speech") if isinstance(plain, Mapping) else None
    return text.strip() if isinstance(text, str) else ""


def _tools_text(speaker_known: bool, *, auto: bool) -> str:
    """How to use the memory tools. Without it models tend to say they cannot remember anything
    between conversations, which is their default belief, and never call the tools."""
    text = (
        "You have a long-term memory that lasts between conversations and restarts. When the user "
        "asks you to remember something, call the remember tool for each fact; when asked to "
        "forget, call forget. Never say you cannot remember things for future conversations: you "
        "can. Facts about the family, pets, guests and the house are the home's; only the "
        "speaker's own preferences and habits are personal."
    )
    if auto:
        text += (
            "\n\nAlso remember on your own, without being asked, lasting facts you learn in "
            "conversation about the home and the family: names and relations, birthdays, pets, "
            "routines, habits and preferences (e.g. 'Grigory goes to bed at 9 pm'). Keep each "
            "short; do not keep what you already know; when a known fact has changed, forget the "
            "old one and remember the new one. Never keep sensitive things: health and medicine, "
            "money, passwords and codes, documents, addresses outside the home, intimate or "
            "private matters, what someone asked to keep to themselves; nor passing things "
            "(today's plans, moods, one-off requests). When you keep something on your own, say "
            "so in a few words."
        )
    if not speaker_known:
        text += "\n\nIt is not known who is speaking now, so keep facts as the home's."
    return text


class LlmAgent:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="llm.agent",
        translations=node_translations(__package__, "llm.agent"),
        version="1.7.0",
        title="LLM agent",
        category="agent",
        inputs={
            "utterance": PortSpec(
                "text.utterance/1", kind="value", description="What the user said or typed."
            ),
            "principal": PortSpec(
                "identity.principal/1",
                kind="value",
                required=False,
                description=(
                    "Who speaks: the client input's, or an identity node's. Not connected: an "
                    "unknown person (no personal memory, no teaching)."
                ),
            ),
        },
        outputs={
            "delta": PortSpec(
                "text.delta/1", description="The answer as it is written: for text-to-speech."
            ),
            "response": PortSpec(
                "agent.response/1", kind="value", description="The whole answer, for the client."
            ),
            "ack": PortSpec(
                "agent.response/1",
                kind="value",
                description=(
                    "The answer, when a done command is confirmed with a sound instead of words: "
                    "connect Client output's ack (with what was done: turned on, off or else). "
                    "Then nothing comes on delta."
                ),
            ),
        },
        config_schema={
            "type": "object",
            GROUPS_KEY: {
                "tools": {
                    "title": "Tools",
                    "description": (
                        "What the agent may do itself: control the home, teach quick commands."
                    ),
                },
                "memory": {
                    "title": "Memory",
                    "description": (
                        "The dialogue, the home's people, facts from the Memory page and what the "
                        "agent remembers itself."
                    ),
                },
            },
            "properties": {
                "base_url": {
                    "title": "API address",
                    "description": (
                        "An OpenAI-compatible API: https://api.openai.com/v1, "
                        "https://openrouter.ai/api/v1, http://<host>:11434/v1 (Ollama)…"
                    ),
                    "type": "string",
                    "default": DEFAULT_URL,
                },
                "api_key": {
                    "title": "API key",
                    "description": "A runtime secret; not needed for a local server.",
                    "type": "secret",
                },
                "model": {
                    "title": "Model",
                    "description": "The model to answer with, as the API names it.",
                    "type": "string",
                    "minLength": 1,
                    NODE_OPTIONS_KEY: True,
                },
                "reasoning": {
                    "title": "Reasoning",
                    "description": (
                        "How much a reasoning model thinks before answering (reasoning_effort). "
                        "Thinking helps with tricky questions but delays the first word; None "
                        "suits voice. Not set: the model's own choice, and nothing is sent, for "
                        "APIs that do not know the setting."
                    ),
                    "enum": ["none", "low", "medium", "high"],
                    ENUM_TITLES_KEY: {
                        "none": "None",
                        "low": "Low",
                        "medium": "Medium",
                        "high": "High",
                    },
                },
                "system_prompt": {
                    "title": "Instructions",
                    "description": (
                        "Who the assistant is and how it answers. {date}, {time}, {weekday} and "
                        "{language} are filled in for every turn."
                    ),
                    "type": "string",
                    "format": "multiline",
                    DYNAMIC_KEY: True,  # e.g. other instructions for a child
                },
                "answer_language": {
                    "title": "Answer language",
                    "description": (
                        "The language the answer says it is in, for text-to-speech and "
                        "conditions. Question: the language speech recognition heard (the "
                        "turn's when it does not tell), known at once. By the answer's text: by "
                        "its alphabet, once the first words have come (they wait for it)."
                    ),
                    "enum": ["question", "text"],
                    ENUM_TITLES_KEY: {
                        "question": "Of the question",
                        "text": "By the answer's text",
                    },
                    "default": "question",
                },
                "history_turns": {
                    "title": "Remembered exchanges",
                    "description": "How many questions and answers of the dialogue to send; 10.",
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 50,
                    "default": 10,
                    GROUP_KEY: "memory",
                },
                "history_hours": {
                    "title": "Dialogue kept, hours",
                    "description": (
                        "After this long without a word the dialogue is forgotten and the next "
                        "question starts afresh; 24. A spoken dialogue in Home Assistant also "
                        "ends after 5 minutes of silence; a Telegram chat lasts until /new."
                    ),
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 720,
                    "default": DEFAULT_HISTORY_HOURS,
                    GROUP_KEY: "memory",
                },
                "memory": {
                    "title": "Memory",
                    "description": (
                        "Facts from the Memory page go into the instructions: the home's, and "
                        "the speaker's own when it is known who speaks."
                    ),
                    "enum": ["personal", "home", "off"],
                    ENUM_TITLES_KEY: {
                        "personal": "The home's and the speaker's",
                        "home": "The home's only",
                        "off": "None",
                    },
                    "default": "personal",
                    GROUP_KEY: "memory",
                },
                "home": {
                    "title": "Controls the home",
                    "description": (
                        "The agent may control and ask about the devices exposed to Assist in "
                        "Home Assistant (Settings → Voice assistants → Expose), with Home "
                        "Assistant's own tools, as the person who speaks; needs a model with "
                        "function calling and FlowPilot Assist 0.1.14 or newer. On when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                    GROUP_KEY: "tools",
                },
                "home_state": {
                    "title": "Home state in the prompt",
                    "description": (
                        "The state of the devices exposed to Assist (on or off, temperature, "
                        "mode) goes into every request, so the model acts at once instead of "
                        "first asking Home Assistant for it: a command is answered a request "
                        "sooner, every request is longer. Needs FlowPilot Assist 0.1.34 or "
                        "newer. On when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                    GROUP_KEY: "tools",
                },
                "acknowledge": {
                    "title": "Confirm with a sound",
                    "description": (
                        "The agent may confirm a simple command it did (turn on the light) with "
                        "the satellite's sound instead of words: it says so with the actions, and "
                        "when they all succeeded the turn ends on ack at once, without a second "
                        "request to the model. In the same room: only when every device it "
                        "touched is in the room of the speaker that heard it (as Home Assistant "
                        "does); else it answers in words. Needs ack connected; in the same room "
                        "when not set."
                    ),
                    "enum": ["area", "model", "never"],
                    ENUM_TITLES_KEY: {
                        "area": "In the same room",
                        "model": "When the model decides",
                        "never": "Never",
                    },
                    "default": "area",
                    GROUP_KEY: "tools",
                },
                "teach": {
                    "title": "Teaches quick commands",
                    "description": (
                        "After doing something with Home Assistant's tools that people will ask "
                        "for again (several devices at once, a nickname), the agent offers to "
                        "remember it as a quick command and saves it when the person agrees; "
                        "Quick commands then do it at once, without an LLM. On when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                    GROUP_KEY: "tools",
                },
                "people": {
                    "title": "Knows the household",
                    "description": (
                        "Who is talking and who lives in the home, with who is home, from Home "
                        "Assistant's people and users; needs FlowPilot Assist 0.1.12 or newer. "
                        "On when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                    GROUP_KEY: "memory",
                },
                "remember": {
                    "title": "Remembers facts",
                    "description": (
                        "Whether the agent keeps and forgets facts on the Memory page itself; "
                        "needs a model with function calling. On its own: it also keeps what it "
                        "learns about the home and the family (names, pets, routines, habits), "
                        "never health, money, passwords or private matters."
                    ),
                    "enum": ["asked", "auto", "off"],
                    ENUM_TITLES_KEY: {
                        "asked": "When asked",
                        "auto": "On its own too",
                        "off": "Never",
                    },
                    "default": "asked",
                    GROUP_KEY: "memory",
                },
                "temperature": {
                    "title": "Temperature",
                    "description": "Lower is steadier, higher is more varied.",
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    DYNAMIC_KEY: True,
                },
                "max_tokens": {
                    "title": "Longest answer, tokens",
                    "description": "Keeps spoken answers short; no limit when not set.",
                    "type": "integer",
                    "minimum": 16,
                    "maximum": 8192,
                },
                "timeout_s": {
                    "title": "Timeout, s",
                    "description": "The longest a turn may wait for the answer; 60 when not set.",
                    "type": "number",
                    "minimum": 5,
                    "maximum": 300,
                    "default": 60,
                },
            },
            "required": ["model"],
            "additionalProperties": False,
        },
        optional_host_ops=frozenset({PEOPLE_OP, HOME_TOOLS_OP, HOME_CALL_OP, SERVICE_CALL_OP}),
        turn_timeout_s=310.0,
        description=(
            "Answers with a language model of any OpenAI-compatible API (OpenAI, OpenRouter, "
            "Ollama, LM Studio…), streaming the answer so speech starts on the first words, and "
            "remembering the dialogue."
        ),
    )

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._key: str | None = None
        self._node = ""
        self._http: aiohttp.ClientSession | None = None
        self._memory: Memory | None = None
        self._commands: QuickCommands | None = None
        self._skills: Skills | None = None
        # the provider refused tools with reasoning on (see _complete_with_tools)
        self._no_reasoning = False

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        if field != "model":
            return []
        timeout = aiohttp.ClientTimeout(total=15)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.get(f"{_base(config)}/models", headers=_headers(_key(config))) as resp,
        ):
            if resp.status != 200:
                raise LlmError(await _error_text(resp))
            data = await resp.json()
        models = sorted(str(m["id"]) for m in data.get("data", []) if m.get("id"))
        return [ConfigOption(m) for m in models]

    async def start(self, ctx: NodeContext) -> None:
        self._commands = getattr(ctx, "commands", None)
        self._skills = getattr(ctx, "skills", None)
        self._config = dict(ctx.config)
        self._node = ctx.node_id
        self._memory = getattr(ctx, "memory", None)
        ref = ctx.config.get("api_key")
        self._key = ctx.secret(ref["secret"]) if isinstance(ref, dict) else None
        timeout = aiohttp.ClientTimeout(total=float(self._config.get("timeout_s", 60)))
        self._http = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.close()

    def _system(self, language: str | None, config: Mapping[str, Any]) -> str:
        now = datetime.now().astimezone()
        values = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "language": language or "",
        }
        prompt = str(config.get("system_prompt") or "")
        for name, value in values.items():
            prompt = prompt.replace("{" + name + "}", value)
        return prompt

    async def _speaker(self, turn: TurnSession) -> str | None:
        """The user id of who speaks, when known: from the ``principal`` input (the client input,
        or an identity node); not connected, an unknown person."""
        principal: Principal | None = await turn.input("principal").value()
        return principal.user_id if principal is not None and principal.is_known else None

    def _known(self, speaker: str | None) -> tuple[list[str], list[str]]:
        """Facts for this turn: the home's, and the speaker's own."""
        mode = self._config.get("memory", "personal")
        if self._memory is None or mode == "off":
            return [], []
        owner = speaker if mode == "personal" else None
        facts = self._memory.facts(owner)
        return (
            [f.text for f in facts if f.owner is None],
            [f.text for f in facts if f.owner is not None and f.owner == owner],
        )

    def _toolbox(self, speaker: str | None) -> Toolbox:
        mode = self._config.get("memory", "personal")
        if self._memory is None or mode == "off" or self._remember() == "off":
            return Toolbox([])
        tools = memory_tools(
            self._memory, speaker, personal=mode == "personal", auto=self._remember() == "auto"
        )
        return Toolbox(tools)

    def _remember(self) -> str:
        return str(self._config.get("remember", "asked"))

    async def _complete(
        self,
        body: dict[str, Any],
        on_text: Callable[[str], Awaitable[None]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """One streamed completion: its text (sent on as it comes) and the tool calls asked for."""
        assert self._http is not None
        pieces: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        async with self._http.post(
            f"{_base(self._config)}/chat/completions", json=body, headers=_headers(self._key)
        ) as resp:
            if resp.status != 200:
                raise LlmError(await _error_text(resp))
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue  # read to the end, so the connection goes back to the pool clean
                chunk = json.loads(data)
                if "error" in chunk:
                    raise LlmError(str(chunk["error"].get("message") or chunk["error"]))
                for choice in chunk.get("choices") or []:
                    step = choice.get("delta") or {}
                    if text := step.get("content"):
                        pieces.append(text)
                        await on_text(text)
                    for part in step.get("tool_calls") or []:
                        call = calls.setdefault(
                            int(part.get("index", 0)), {"id": "", "name": "", "arguments": ""}
                        )
                        call["id"] = part.get("id") or call["id"]
                        function = part.get("function") or {}
                        call["name"] += function.get("name") or ""
                        call["arguments"] += function.get("arguments") or ""
        return "".join(pieces), [calls[i] for i in sorted(calls)]

    async def _home(
        self,
        turn: TurnSession,
        speaker: str | None,
        language: str | None,
        done: list[dict[str, Any]],
        said: list[str],
    ) -> tuple[str, list[Tool], str]:
        """Home Assistant's instructions, tools and (with ``home_state``) the state of its
        exposed devices for this turn, or nothing without them.

        Each action that succeeded is added to ``done``: its tool, words and whether its devices
        are in the satellite's room (for a confirmation sound). A tool that answers with the words
        for the person (``say``, e.g. "Включаю Сплин" from ``play_music``) adds them to ``said``."""
        if not self._config.get("home", True):
            return "", [], ""
        device = turn.context.device
        who: dict[str, Any] = {
            "user_id": speaker,
            "device_id": device.device_id if device else None,
            "language": language,
        }
        try:
            async with asyncio.timeout(HOME_TOOLS_TIMEOUT_S):
                live = bool(self._config.get("home_state", True))
                result = await turn.call_host(HOME_TOOLS_OP, {**who, "live_context": live})
        except HostUnavailable:
            return "", [], ""
        except (HostError, TimeoutError) as err:
            turn.trace("llm.agent.home_failed", reason=str(err)[:200])
            return "", [], ""
        if not isinstance(result, Mapping):
            return "", [], ""

        def tool(spec: Mapping[str, Any]) -> Tool:
            name = str(spec["name"])

            async def run(args: Mapping[str, Any]) -> str:
                try:
                    async with asyncio.timeout(HOME_CALL_TIMEOUT_S):
                        answer = await turn.call_host(
                            HOME_CALL_OP, {**who, "name": name, "arguments": dict(args)}
                        )
                except (HostError, HostUnavailable, TimeoutError) as err:
                    raise ValueError(f"Home Assistant did not run it: {err}") from err
                data = json.dumps(answer.get("data"), ensure_ascii=False)
                if answer.get("error"):
                    raise ValueError(data)
                if words := str(answer.get("say") or "").strip():
                    said.append(words)
                result = answer.get("data")
                if isinstance(result, Mapping) and result.get("response_type") == "action_done":
                    done.append(
                        {
                            "tool": name,
                            "speech": _speech_of(result),
                            "here": bool(answer.get("targets_in_area")),
                        }
                    )
                return data

            parameters = spec.get("parameters")
            return Tool(
                name,
                str(spec.get("description") or ""),
                parameters if isinstance(parameters, Mapping) else {"type": "object"},
                run,
            )

        specs = [t for t in result.get("tools") or [] if isinstance(t, Mapping) and t.get("name")]
        tools = [tool(t) for t in specs]
        tools += [self._skill_tool(turn, s, who, done) for s in self._enabled_skills()]
        return str(result.get("prompt") or ""), tools, str(result.get("live_context") or "")

    def _enabled_skills(self) -> list[Skill]:
        return [s for s in self._skills.all() if s.enabled] if self._skills is not None else []

    def _skill_tool(
        self, turn: TurnSession, skill: Skill, who: dict[str, Any], done: list[dict[str, Any]]
    ) -> Tool:
        """A skill as a tool: the model fills its fields, Home Assistant runs its actions."""

        async def run(args: Mapping[str, Any]) -> str:
            results = await run_skill(turn, skill, args, who)
            done.append({"tool": skill.tool, "speech": "", "here": False})
            return json.dumps(results, ensure_ascii=False)

        spec = skill_definition(skill)["function"]
        return Tool(spec["name"], spec["description"], spec["parameters"], run)

    async def _people(self, turn: TurnSession) -> list[Mapping[str, Any]] | None:
        """The home's people from the turn's client, or None without one (or when turned off)."""
        if not self._config.get("people", True):
            return None
        try:
            async with asyncio.timeout(PEOPLE_TIMEOUT_S):
                result = await turn.call_host(PEOPLE_OP, {})
        except HostUnavailable:
            return None
        except (HostError, TimeoutError) as err:
            turn.trace("llm.agent.people_failed", reason=str(err)[:200])
            return None
        people = result.get("people") if isinstance(result, Mapping) else None
        return [p for p in people if isinstance(p, Mapping)] if isinstance(people, list) else None

    async def _dialogue(self, turn: TurnSession) -> list[dict[str, Any]]:
        """The session's shared dialogue; a dialogue this node kept for itself before 0.1.17
        moves there once."""
        kept = await dialogue.exchanges(turn.shared)
        own_key = f"history:{self._node}"
        own = await turn.state.get(own_key)
        if not kept and isinstance(own, list) and own:
            for user, answer in zip(own[::2], own[1::2], strict=False):
                await dialogue.add(
                    turn.shared, str(user.get("content")), str(answer.get("content")), by=self._node
                )
            kept = await dialogue.exchanges(turn.shared)
        if own is not None:
            await turn.state.delete(own_key)
        return kept

    async def _complete_with_tools(
        self,
        turn: TurnSession,
        body: dict[str, Any],
        request: dict[str, Any],
        on_text: Callable[[str], Awaitable[None]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """``_complete``; when the provider takes tools only without reasoning (OpenAI's reasoning
        models in Chat Completions), turn reasoning off for this node from now on and ask again."""
        try:
            return await self._complete(request, on_text)
        except LlmError as err:
            text = str(err)
            effort = request.get("reasoning_effort")
            if "tools" not in request or "reasoning_effort" not in text or effort == "none":
                raise
            turn.trace("llm.agent.no_reasoning", reason=text[:200])
            Journal(turn)("llm.no_reasoning", "tools need reasoning off: asking again", text)
            self._no_reasoning = True
            body["reasoning_effort"] = "none"
            return await self._complete(request | {"reasoning_effort": "none"}, on_text)

    async def run_turn(self, turn: TurnSession) -> None:
        said: Utterance | None = await turn.input("utterance").value()
        cfg = await turn.settings(self._config)  # instructions or temperature for this turn
        if said is None or not said.text.strip():
            return
        language = said.language or turn.context.language
        speaker = await self._speaker(turn)
        home, own = self._known(speaker)
        toolbox = self._toolbox(speaker)
        remembers = bool(toolbox)
        keep = int(self._config.get("history_turns", 10))
        quiet_s = 3600.0 * int(self._config.get("history_hours", DEFAULT_HISTORY_HOURS))
        earlier = dialogue.recent(await self._dialogue(turn), turns=keep, quiet_s=quiet_s)
        history: list[dict[str, str]] = [
            message
            for e in earlier
            for message in (
                {"role": "user", "content": str(e.get("user") or "")},
                {"role": "assistant", "content": str(e.get("assistant") or "")},
            )
        ]
        personal = self._config.get("memory", "personal") == "personal"
        auto = self._remember() == "auto"
        guide = _tools_text(speaker is not None or not personal, auto=auto) if remembers else ""
        done: list[dict[str, Any]] = []  # the actions that succeeded, in order
        tool_words: list[str] = []  # the words tools gave for the person (see ``_home``)
        people, (home_prompt, home_tools, home_state) = await asyncio.gather(
            self._people(turn), self._home(turn, speaker, language, done, tool_words)
        )
        if home_tools:
            toolbox = toolbox.with_tools(home_tools)
        mode = str(self._config.get("acknowledge", "area"))
        confirming = bool(home_tools) and mode != "never" and turn.output("ack").connected
        if confirming:
            toolbox = toolbox.with_tools([confirm_tool(mode)])
        home_names = {t.name for t in home_tools}
        teaching = (
            bool(home_tools) and self._commands is not None and self._config.get("teach", True)
        )
        earlier_calls: list[Any] = list(await turn.state.get(LAST_CALLS_KEY) or [])
        teach_text = ""
        if teaching and self._commands is not None:
            toolbox = toolbox.with_tools(command_tools(self._commands, speaker, home_names))
            teach_text = TEACH_TEXT
            if earlier_calls:
                teach_text += (
                    "\n\nThe Home Assistant tool calls of your previous answer (to save them): "
                    + json.dumps(earlier_calls, ensure_ascii=False)
                )
        log = Journal(turn)
        who = next(
            (str(p.get("name")) for p in people or [] if speaker and p.get("user_id") == speaker),
            speaker,
        )
        log(
            "memory",
            f"{len(home)} facts of the home, {len(own)} of {who or 'nobody known'}; "
            f"dialogue: {len(history) // 2} earlier exchanges",
            "\n".join([*(f"home: {t}" for t in home), *(f"{who}: {t}" for t in own)]) or None,
            home=len(home),
            own=len(own),
            history=len(history) // 2,
        )
        parts = (
            self._system(language, cfg),
            _people_text(people, speaker),
            _memory_text(home, own),
            home_prompt if home_tools else "",
            teach_text,
        )
        # last: it changes every turn, the rest stays the same and is cached by the provider
        state = STATE_TEXT.format(home_state) if home_tools and home_state else ""
        system = "\n\n".join(p for p in (*parts, guide, state) if p)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        messages += history[-2 * keep :] if keep else []
        messages.append({"role": "user", "content": said.text})
        log(
            "context",
            f"speaker: {who or 'unknown'}; people: {len(people or [])}; tools: {len(toolbox)} "
            f"({len(home_tools)} from Home Assistant)",
            system or None,
            tools=len(toolbox),
        )
        body: dict[str, Any] = {"model": self._config["model"], "stream": True}
        if "temperature" in cfg:
            body["temperature"] = float(cfg["temperature"])
        if "max_tokens" in self._config:
            body["max_tokens"] = int(self._config["max_tokens"])
        if "reasoning" in self._config:
            body["reasoning_effort"] = str(self._config["reasoning"])
        if self._no_reasoning and toolbox:
            body["reasoning_effort"] = "none"

        started = time.monotonic()
        first_ms: int | None = None
        pieces: list[str] = []
        delta = turn.output("delta")
        by_text = self._config.get("answer_language", "question") == "text"
        prefer = (language, turn.context.language)
        held: list[str] = []  # the first words, until their language is known (by_text)
        opened = False

        async def speak(text: str, *, last: bool = False) -> None:
            """Pass text on; the answer opens with its language in the header."""
            nonlocal opened
            if not opened:
                spoken = language
                if by_text:
                    held.append(text)
                    text = "".join(held)
                    if not last and not _language_clear(text):
                        return
                    held.clear()
                    spoken = language_by_script(text, prefer) or language
                await delta.open({LANGUAGE_META: spoken} if spoken else None)
                opened = True
            if text:
                await delta.send(TextDelta(text))

        gap = False  # a new round after tool calls: keep its words apart from the earlier ones

        async def on_text(text: str) -> None:
            nonlocal first_ms, gap
            if first_ms is None:
                first_ms = round(1000 * (time.monotonic() - started))
            if gap and pieces and not pieces[-1][-1:].isspace() and not text[:1].isspace():
                text = " " + text
            gap = False
            pieces.append(text)
            await speak(text)

        used: list[str] = []
        home_calls: list[dict[str, Any]] = []
        acknowledged: str | None = None  # the sound that confirms the turn, instead of words
        told = False  # the answer is the words the tools gave: no round to ask the model for them
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            request = body | {"messages": messages}
            offer = bool(toolbox) and round_ < MAX_TOOL_ROUNDS
            if offer:
                request["tools"] = toolbox.definitions()
            log(
                "llm.request",
                f"round {round_ + 1}: {len(messages)} messages, "
                f"{len(request.get('tools') or [])} tools, model {body['model']}"
                + (f", reasoning {body['reasoning_effort']}" if "reasoning_effort" in body else ""),
                said.text if round_ == 0 else None,
                round=round_ + 1,
            )
            asked_at = time.monotonic()
            text_before = len(pieces)
            try:
                text, calls = await self._complete_with_tools(turn, body, request, on_text)
            except LlmError as err:
                if not offer or "tool" not in str(err).lower():
                    raise
                # a model without function calling: answer without tools from now on
                turn.trace("llm.agent.no_tools", reason=str(err)[:200])
                log("llm.no_tools", "the model cannot call tools: answering without them", str(err))
                toolbox = Toolbox([])
                body.pop("reasoning_effort", None)
                if guide or home_tools:  # it cannot use tools after all: do not tell it it can
                    plain = "\n\n".join(p for p in parts[:3] if p)
                    messages = [{"role": "system", "content": plain}] if plain else []
                    messages += history[-2 * keep :] if keep else []
                    messages.append({"role": "user", "content": said.text})
                text, calls = await self._complete(body | {"messages": messages}, on_text)
            names = [c["name"] for c in calls]
            log(
                "llm.response",
                f"round {round_ + 1}: {len(text)} chars"
                + (f", calls {', '.join(names)}" if names else ""),
                "".join(pieces[text_before:]).strip() or None,
                round=round_ + 1,
                ms=round(1000 * (time.monotonic() - asked_at)),
            )
            if not calls:
                break
            gap = True
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c["id"] or f"call_{round_}_{i}",
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        }
                        for i, c in enumerate(calls)
                    ],
                }
            )
            acted_from = len(done)
            said_from = len(tool_words)
            failed = False
            worded = 0  # calls of this reply that gave the words for the person
            # the confirmation is decided last, once the actions of this reply have run
            order = sorted(range(len(calls)), key=lambda i: calls[i]["name"] == CONFIRM_TOOL)
            for i in order:
                call = calls[i]
                called_at = time.monotonic()
                if call["name"] == CONFIRM_TOOL and confirming:
                    sound, result = self._confirmation(
                        mode, done[acted_from:], failed=failed, spoken=len(pieces) > 0
                    )
                    ok = sound is not None
                    if ok:
                        acknowledged = sound
                else:
                    before = len(tool_words)
                    result, ok = await toolbox.call(call["name"], call["arguments"])
                    failed = failed or not ok
                    worded += int(ok and len(tool_words) > before)
                if ok and call["name"] in home_names:
                    try:
                        arguments = json.loads(call["arguments"] or "{}")
                    except ValueError:
                        arguments = {}
                    home_calls.append({"tool": call["name"], "arguments": arguments})
                used.append(call["name"] if ok else f"{call['name']}!")
                turn.trace("llm.agent.tool", tool=call["name"], ok=ok)
                log(
                    "tool",
                    f"{call['name']}: {'ok' if ok else 'failed'}",
                    _tool_detail(call["arguments"], result),
                    tool=call["name"],
                    ok=ok,
                    ms=round(1000 * (time.monotonic() - called_at)),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"] or f"call_{round_}_{i}",
                        "content": result,
                    }
                )
            if acknowledged is not None:
                break  # confirmed with a sound: no words to ask the model for
            if worded and worded == len(calls) and not failed:
                # every call of the reply gave its words ("Включаю Сплин"): say them and end,
                # instead of a round that only asks the model to say the same
                if not "".join(pieces[text_before:]).strip():  # unless it spoke already
                    for words in dict.fromkeys(tool_words[said_from:]):
                        await on_text(words if words[-1:] in ".!?" else f"{words}.")
                        gap = True
                told = True
                log("told", "answered with the tools' words", " ".join(tool_words[said_from:]))
                break
        if held:
            await speak("", last=True)
        await delta.end()
        answer = "".join(pieces).strip()
        if by_text and answer:
            language = language_by_script(answer, prefer) or language
        if acknowledged is not None:
            base = (language or "").split("-")[0].casefold()
            acted = [d["speech"] for d in done if d["speech"]]
            answer = " ".join(dict.fromkeys(acted)) or DONE.get(base, DONE["en"])
            log("acknowledge", f"confirmed with a sound ({acknowledged})", answer)
        turn.trace(
            "llm.agent",
            model=str(self._config["model"]),
            first_token_ms=first_ms,
            total_ms=round(1000 * (time.monotonic() - started)),
            chars=len(answer),
            history=len(history) // 2,
            facts=len(home) + len(own),
            people=len(people) if people is not None else None,
            tools=",".join(used) or None,
            acknowledge=acknowledged,
            told=told,
        )
        if not answer:
            raise LlmError("the model gave an empty answer")
        await dialogue.add(turn.shared, said.text, answer, by=self._node)
        # what this answer did with Home Assistant, for "yes, remember it" in the next turn
        await turn.state.set(LAST_CALLS_KEY, home_calls, ttl_s=LAST_CALLS_S)
        response = Response(text=answer, language=language)
        if acknowledged is not None:  # a sound instead of words, as Home Assistant's pipeline does
            await turn.output("ack").send(replace(response, data={"acknowledge": acknowledged}))
        await turn.output("response").send(response)

    @staticmethod
    def _confirmation(
        mode: str, acted: list[dict[str, Any]], *, failed: bool, spoken: bool
    ) -> tuple[str | None, str]:
        """The sound that confirms this reply's actions, or None, and what the model is told."""
        if failed:
            return None, "no sound: an action failed; say in words what happened"
        if not acted:
            return None, "no sound: nothing was done in this reply; answer in words"
        if spoken:
            return None, "no sound: you already answered in words; finish in words"
        if mode == "area" and not all(a["here"] for a in acted):
            return None, (
                "no sound: not every device is in the room of the speaker that heard it; "
                "say in a few words what you did"
            )
        return acknowledge_kind([a["tool"] for a in acted]), "confirmed with a sound"
