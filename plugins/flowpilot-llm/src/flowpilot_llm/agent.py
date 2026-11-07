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
from datetime import datetime
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    GROUP_KEY,
    GROUPS_KEY,
    NODE_OPTIONS_KEY,
    ConfigOption,
    HostError,
    HostUnavailable,
    Memory,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    Response,
    TextDelta,
    TurnSession,
    Utterance,
    node_translations,
)

from .tools import Tool, Toolbox, memory_tools

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_HISTORY_HOURS = 24
# Who lives in the home, from the turn's client (Home Assistant: people and users)
PEOPLE_OP = "ha.people.list"
PEOPLE_TIMEOUT_S = 2.0
# Home Assistant's Assist LLM API: instructions and tools for the entities exposed to Assist
HOME_TOOLS_OP = "ha.llm.tools"
HOME_CALL_OP = "ha.llm.call"
HOME_TOOLS_TIMEOUT_S = 3.0
HOME_CALL_TIMEOUT_S = 20.0
# Rounds of tool calls in one turn; the last round must answer in words.
MAX_TOOL_ROUNDS = 4


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
        version="1.4.0",
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
                    "Who is speaking, from an identity node; without it, the user Home Assistant "
                    "names (the signed-in user, a mapped Telegram user)."
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
        },
        config_schema={
            "type": "object",
            GROUPS_KEY: {
                "tools": {
                    "title": "Tools",
                    "description": "What the agent may do itself: control the home.",
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
        optional_host_ops=frozenset({PEOPLE_OP, HOME_TOOLS_OP, HOME_CALL_OP}),
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

    def _system(self, language: str | None) -> str:
        now = datetime.now().astimezone()
        values = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "language": language or "",
        }
        prompt = str(self._config.get("system_prompt") or "")
        for name, value in values.items():
            prompt = prompt.replace("{" + name + "}", value)
        return prompt

    async def _speaker(self, turn: TurnSession) -> str | None:
        """The user id of who speaks, when known: an identity node's word, else the client's."""
        principal: Principal | None = None
        if turn.input("principal").connected:
            principal = await turn.input("principal").value()
        principal = principal or turn.context.initial_principal
        return principal.user_id if principal.is_known else None

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
        self, turn: TurnSession, speaker: str | None, language: str | None
    ) -> tuple[str, list[Tool]]:
        """Home Assistant's instructions and tools for this turn, or nothing without them."""
        if not self._config.get("home", True):
            return "", []
        device = turn.context.device
        who: dict[str, Any] = {
            "user_id": speaker,
            "device_id": device.device_id if device else None,
            "language": language,
        }
        try:
            async with asyncio.timeout(HOME_TOOLS_TIMEOUT_S):
                result = await turn.call_host(HOME_TOOLS_OP, who)
        except HostUnavailable:
            return "", []
        except (HostError, TimeoutError) as err:
            turn.trace("llm.agent.home_failed", reason=str(err)[:200])
            return "", []
        if not isinstance(result, Mapping):
            return "", []

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
        return str(result.get("prompt") or ""), tools

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
            self._no_reasoning = True
            body["reasoning_effort"] = "none"
            return await self._complete(request | {"reasoning_effort": "none"}, on_text)

    async def run_turn(self, turn: TurnSession) -> None:
        said: Utterance | None = await turn.input("utterance").value()
        if said is None or not said.text.strip():
            return
        language = said.language or turn.context.language
        speaker = await self._speaker(turn)
        home, own = self._known(speaker)
        toolbox = self._toolbox(speaker)
        remembers = bool(toolbox)
        history_key = f"history:{self._node}"
        keep = int(self._config.get("history_turns", 10))
        history: list[dict[str, str]] = list(await turn.state.get(history_key) or [])
        personal = self._config.get("memory", "personal") == "personal"
        auto = self._remember() == "auto"
        guide = _tools_text(speaker is not None or not personal, auto=auto) if remembers else ""
        people, (home_prompt, home_tools) = await asyncio.gather(
            self._people(turn), self._home(turn, speaker, language)
        )
        if home_tools:
            toolbox = toolbox.with_tools(home_tools)
        parts = (
            self._system(language),
            _people_text(people, speaker),
            _memory_text(home, own),
            home_prompt if home_tools else "",
        )
        system = "\n\n".join(p for p in (*parts, guide) if p)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        messages += history[-2 * keep :] if keep else []
        messages.append({"role": "user", "content": said.text})
        body: dict[str, Any] = {"model": self._config["model"], "stream": True}
        if "temperature" in self._config:
            body["temperature"] = float(self._config["temperature"])
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

        gap = False  # a new round after tool calls: keep its words apart from the earlier ones

        async def on_text(text: str) -> None:
            nonlocal first_ms, gap
            if first_ms is None:
                first_ms = round(1000 * (time.monotonic() - started))
            if gap and pieces and not pieces[-1][-1:].isspace() and not text[:1].isspace():
                text = " " + text
            gap = False
            pieces.append(text)
            await delta.send(TextDelta(text))

        used: list[str] = []
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            request = body | {"messages": messages}
            offer = bool(toolbox) and round_ < MAX_TOOL_ROUNDS
            if offer:
                request["tools"] = toolbox.definitions()
            try:
                text, calls = await self._complete_with_tools(turn, body, request, on_text)
            except LlmError as err:
                if not offer or "tool" not in str(err).lower():
                    raise
                # a model without function calling: answer without tools from now on
                turn.trace("llm.agent.no_tools", reason=str(err)[:200])
                toolbox = Toolbox([])
                body.pop("reasoning_effort", None)
                if guide or home_tools:  # it cannot use tools after all: do not tell it it can
                    plain = "\n\n".join(p for p in parts[:3] if p)
                    messages = [{"role": "system", "content": plain}] if plain else []
                    messages += history[-2 * keep :] if keep else []
                    messages.append({"role": "user", "content": said.text})
                text, calls = await self._complete(body | {"messages": messages}, on_text)
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
            for i, call in enumerate(calls):
                result, ok = await toolbox.call(call["name"], call["arguments"])
                used.append(call["name"] if ok else f"{call['name']}!")
                turn.trace("llm.agent.tool", tool=call["name"], ok=ok)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"] or f"call_{round_}_{i}",
                        "content": result,
                    }
                )
        await delta.end()
        answer = "".join(pieces).strip()
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
        )
        if not answer:
            raise LlmError("the model gave an empty answer")
        if keep:
            history += [
                {"role": "user", "content": said.text},
                {"role": "assistant", "content": answer},
            ]
            await turn.state.set(
                history_key,
                history[-2 * keep :],
                ttl_s=3600.0 * int(self._config.get("history_hours", DEFAULT_HISTORY_HOURS)),
            )
        await turn.output("response").send(Response(text=answer, language=language))
