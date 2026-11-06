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

import json
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    NODE_OPTIONS_KEY,
    ConfigOption,
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

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_HISTORY_HOURS = 24


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


class LlmAgent:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="llm.agent",
        translations=node_translations(__package__, "llm.agent"),
        version="1.0.0",
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

    async def run_turn(self, turn: TurnSession) -> None:
        said: Utterance | None = await turn.input("utterance").value()
        if said is None or not said.text.strip():
            return
        language = said.language or turn.context.language
        home, own = self._known(await self._speaker(turn))
        history_key = f"history:{self._node}"
        keep = int(self._config.get("history_turns", 10))
        history: list[dict[str, str]] = list(await turn.state.get(history_key) or [])
        system = "\n\n".join(p for p in (self._system(language), _memory_text(home, own)) if p)
        messages = [{"role": "system", "content": system}] if system else []
        messages += history[-2 * keep :] if keep else []
        messages.append({"role": "user", "content": said.text})
        body: dict[str, Any] = {
            "model": self._config["model"],
            "messages": messages,
            "stream": True,
        }
        if "temperature" in self._config:
            body["temperature"] = float(self._config["temperature"])
        if "max_tokens" in self._config:
            body["max_tokens"] = int(self._config["max_tokens"])

        started = time.monotonic()
        first_ms: int | None = None
        pieces: list[str] = []
        delta = turn.output("delta")
        assert self._http is not None
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
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        if first_ms is None:
                            first_ms = round(1000 * (time.monotonic() - started))
                        pieces.append(text)
                        await delta.send(TextDelta(text))
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
