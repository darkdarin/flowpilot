"""Home Assistant's quick commands: the built-in agent first, an LLM only for the rest.

Home Assistant's own conversation agent matches sentences ("turn on the kitchen light") in tens of
milliseconds and runs them; an LLM takes seconds. This node hands the utterance to it: what it
understood and did comes out as the answer (and its speech), what it did not passes on unchanged
on ``unhandled``, to an agent. Join the two answers with First present in front of the client
output and text-to-speech. The exchange goes into the session's dialogue, so the agent knows
what was done ("turn it back on").
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, ClassVar

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    LOG_EVENT,
    OPTIONS_KEY,
    HostError,
    HostUnavailable,
    NodeContext,
    NodeSpec,
    PortSpec,
    QuickCommand,
    QuickCommands,
    Response,
    TextDelta,
    TurnSession,
    Utterance,
    dialogue,
    node_translations,
)

from .names import NameMatcher, phrase_key, same_phrase, warm_up, words
from .nodes import HA_CONVERSATION_OP

NAMES_OP = "ha.names.list"
# Home Assistant's tools, as the LLM agent calls them: learned commands replay their calls
TOOL_CALL_OP = "ha.llm.call"
# what a learned command answers when it has no reply of its own, and when it failed
DONE = {"ru": ("Готово", "Не получилось"), "en": ("Done", "That did not work")}


def _base(language: str | None) -> str:
    return (language or "").split("-")[0].casefold()


# Names change rarely (a new device, a renamed room): asked for again after this long
NAMES_MAX_AGE_S = 300.0

TYPE = "ha.quick"

# Words that make Home Assistant's sentence matching miss a command it knows: politeness, fillers,
# ways to address the assistant. Removed (as whole words) for a second try.
FILLERS: dict[str, tuple[str, ...]] = {
    "ru": (
        "пожалуйста", "слушай", "послушай", "а можешь", "ты можешь", "можешь", "можно",
        "будь добр", "будь добра", "будь любезен", "будь любезна", "сделай так чтобы",
        "давай", "ну", "а", "ещё", "еще", "уже", "плиз",
    ),
    "en": (
        "please", "could you", "can you", "would you", "will you", "hey", "just", "kindly",
        "i want you to", "go ahead and",
    ),
}  # fmt: skip


def cleaned(text: str, language: str | None, extra: list[str]) -> str:
    """``text`` without punctuation, fillers and politeness, as Home Assistant's sentences are
    written: "Алиса, включи-ка, пожалуйста, свет на кухне!" → "включи свет на кухне"."""
    words = re.sub(r"[^\w\s-]", " ", text.casefold().replace("ё", "е"))
    words = re.sub(r"(\w)-(ка|то)\b", r"\1", words)  # включи-ка → включи
    words = " ".join(words.replace("-", " ").split())
    base = (language or "").split("-")[0].casefold()
    drop = sorted({*FILLERS.get(base, ()), *(w.casefold() for w in extra)}, key=len, reverse=True)
    for phrase in drop:
        phrase = " ".join(phrase.replace("ё", "е").split())
        if phrase:
            words = re.sub(rf"(?:^|\s){re.escape(phrase)}(?=\s|$)", " ", words)
    return " ".join(words.split())


BUILT_IN_AGENT = "conversation.home_assistant"
UTTERANCE = "text.utterance/1"


class HaQuickCommands:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Home Assistant quick commands",
        category="agent",
        inputs={
            "utterance": PortSpec(
                UTTERANCE, kind="value", description="What the user said or typed."
            ),
        },
        outputs={
            "response": PortSpec(
                "agent.response/1",
                kind="value",
                description="Home Assistant's answer, when it understood and did the command.",
            ),
            "delta": PortSpec(
                "text.delta/1", description="The same answer as text for speech synthesis."
            ),
            "ack": PortSpec(
                "agent.response/1",
                kind="value",
                description=(
                    "The answer, when it is confirmed with a sound instead of words: connect "
                    "Sound signal. Then nothing comes on delta."
                ),
            ),
            "unhandled": PortSpec(
                UTTERANCE,
                kind="value",
                description="The utterance, unchanged, when Home Assistant did not handle it.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "title": "Agent",
                    "description": (
                        "The conversation agent that matches sentences; Home Assistant's own "
                        "when not set. Your own sentences (custom_sentences) work with it too."
                    ),
                    "type": "string",
                    "minLength": 1,
                    OPTIONS_KEY: "ha.conversation",
                },
                "acknowledge": {
                    "title": "Confirm with a sound",
                    "description": (
                        "Instead of speaking the answer, send it to ack for a Sound signal: when "
                        "every device the command touched is in the room of the speaker that "
                        "heard it (as Home Assistant does), always, or never. Needs ack "
                        "connected; in the same room when not set."
                    ),
                    "enum": ["area", "always", "never"],
                    ENUM_TITLES_KEY: {
                        "area": "In the same room",
                        "always": "Always",
                        "never": "Never",
                    },
                    "default": "area",
                },
                "retry_cleaned": {
                    "title": "Second try without extra words",
                    "description": (
                        "When Home Assistant did not understand the phrase, try once more "
                        "without punctuation, politeness and fillers (please, listen, can you…) "
                        "and the words below. On when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "extra_words": {
                    "title": "Words to leave out",
                    "description": (
                        "More words or phrases to remove for the second try, e.g. the "
                        "assistant's name if speech-to-text keeps it."
                    ),
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "additionalProperties": False,
        },
        host_ops=frozenset({HA_CONVERSATION_OP}),
        optional_host_ops=frozenset({NAMES_OP, TOOL_CALL_OP}),
        turn_timeout_s=20.0,
        description=(
            "Runs commands Home Assistant understands by itself (turn on the light, set the "
            "temperature, start a timer) in a fraction of a second, without an LLM; everything "
            "else goes on unchanged to the next agent. Join their answers with First present."
        ),
    )

    def __init__(self) -> None:
        self._agent = BUILT_IN_AGENT
        self._node = ""
        self._retry = True
        self._acknowledge = "area"
        self._extra: list[str] = []
        self._commands: QuickCommands | None = None

    async def start(self, ctx: NodeContext) -> None:
        self._agent = str(ctx.config.get("agent_id") or BUILT_IN_AGENT)
        self._node = ctx.node_id
        self._retry = bool(ctx.config.get("retry_cleaned", True))
        self._acknowledge = str(ctx.config.get("acknowledge", "area"))
        self._extra = [str(w) for w in ctx.config.get("extra_words") or []]
        self._names: tuple[float, list[str]] | None = None
        self._commands = getattr(ctx, "commands", None)
        if self._retry or self._commands is not None:
            await asyncio.to_thread(warm_up)

    async def _matcher(self, turn: TurnSession, language: str | None) -> NameMatcher | None:
        """Names Assist knows, from Home Assistant; kept for a while, they change rarely."""
        now = time.monotonic()
        if self._names is None or now - self._names[0] > NAMES_MAX_AGE_S:
            try:
                result = await turn.call_host(NAMES_OP, {})
            except HostError, HostUnavailable:
                return None
            names = result.get("names") if isinstance(result, dict) else None
            self._names = (now, [str(n) for n in names] if isinstance(names, list) else [])
        return NameMatcher(self._names[1], language) if self._names[1] else None

    async def _ask(self, turn: TurnSession, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await turn.call_host(HA_CONVERSATION_OP, args)
        except (HostError, HostUnavailable) as err:
            # no Home Assistant to ask: the agent after this node answers instead
            return {"response_type": "error", "error_code": "unavailable", "speech": str(err)}
        return dict(result) if isinstance(result, dict) else {}

    async def stop(self) -> None:
        return None

    def _learned(self, text: str, language: str | None) -> QuickCommand | None:
        """A command people taught whose phrase is this one, in any form of its words."""
        if self._commands is None:
            return None
        said = phrase_key(cleaned(text, language, self._extra), language)
        if not said:
            return None
        for command in self._commands.all():
            for phrase in command.phrases:
                if same_phrase(said, phrase_key(cleaned(phrase, language, self._extra), language)):
                    return command
        return None

    async def _replay(
        self, turn: TurnSession, command: QuickCommand, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Run a learned command's actions with Home Assistant's tools, in order; the result
        reads like Home Assistant's own answer."""
        principal = turn.context.initial_principal
        who = {
            "user_id": principal.user_id if principal.is_known else None,
            "device_id": args["device_id"],
            "satellite_id": args["satellite_id"],
            "language": args["language"],
        }
        here = True
        for action in command.actions:
            try:
                result = await turn.call_host(
                    TOOL_CALL_OP, {**who, "name": action.tool, "arguments": dict(action.arguments)}
                )
            except (HostError, HostUnavailable) as err:
                result = {"error": True, "data": {"message": str(err)}}
            if not isinstance(result, dict) or result.get("error"):
                data = result.get("data") if isinstance(result, dict) else None
                why = data.get("message") if isinstance(data, dict) else None
                failed = DONE.get(_base(args["language"]), DONE["en"])[1]
                return {"response_type": "error", "speech": f"{failed}: {why or action.tool}"}
            here = here and bool(result.get("targets_in_area"))
        done = command.reply or DONE.get(_base(args["language"]), DONE["en"])[0]
        return {"response_type": "action_done", "speech": done, "targets_in_area": here}

    async def run_turn(self, turn: TurnSession) -> None:
        said: Utterance | None = await turn.input("utterance").value()
        if said is None or not said.text.strip():
            return
        ctx = turn.context
        args: dict[str, Any] = {
            "text": said.text,
            "language": said.language or ctx.language,
            "agent_id": self._agent,
            "conversation_id": ctx.external.get("ha.conversation_id"),
            "device_id": ctx.device.device_id if ctx.device else None,
            "satellite_id": ctx.device.satellite_id if ctx.device else None,
        }
        started = time.monotonic()
        learned = self._learned(said.text, args["language"])
        if learned is not None:
            result = await self._replay(turn, learned, args)
            if self._commands is not None and result.get("response_type") == "action_done":
                self._commands.used(learned.id)
        else:
            result = await self._ask(turn, args)
        tried = said.text
        code0 = result.get("error_code")
        if learned is None and code0 in ("no_intent_match", "no_valid_targets") and self._retry:
            simpler = cleaned(said.text, args["language"], self._extra)
            matcher = await self._matcher(turn, args["language"])
            named = matcher.canonical(simpler) if matcher else simpler
            # not understood: any cleaning may help; understood but no such target: only a name
            # put back in its own form can
            worth = (
                named != " ".join(words(said.text))
                if code0 == "no_intent_match"
                else (words(named) != words(simpler))
            )
            if named and worth:
                tried = named
                result = await self._ask(turn, args | {"text": named})
        ms = round(1000 * (time.monotonic() - started))
        kind = str(result.get("response_type") or "")
        code = result.get("error_code")
        speech = str(result.get("speech") or "").strip()
        handled = learned is not None or (kind != "error" and bool(speech or kind == "action_done"))
        retried = tried != said.text
        turn.trace(
            "ha.quick",
            handled=handled,
            response=kind,
            code=code,
            ms=ms,
            retried=retried,
            learned=learned.id if learned else None,
        )
        if getattr(turn, "watched", False):
            if learned is not None:
                summary = (
                    f"learned command «{learned.phrases[0]}»: {len(learned.actions)} actions, "
                    f"{kind} in {ms} ms"
                )
            else:
                summary = (
                    f"done by Home Assistant ({kind}) in {ms} ms"
                    if handled
                    else f"passed on ({code or kind or 'no answer'}) after {ms} ms"
                ) + (f"; second try: «{tried}»" if retried else "")
            turn.trace(
                LOG_EVENT, kind="quick", summary=summary, detail=speech or None, ms=ms, ok=handled
            )
        if not handled:
            await turn.output("unhandled").send(said)
            return
        language = args["language"]
        await dialogue.add(turn.shared, said.text, speech, by=self._node)
        answer = Response(
            text=speech,
            language=language,
            continue_conversation=bool(result.get("continue_conversation", False)),
        )
        here = bool(result.get("targets_in_area"))
        sound = (
            kind == "action_done"
            and turn.output("ack").connected
            and (self._acknowledge == "always" or (self._acknowledge == "area" and here))
        )
        if sound:  # a sound instead of words, as Home Assistant's own pipeline does
            await turn.output("ack").send(answer)
        elif speech:
            await turn.output("delta").send(TextDelta(speech))
        await turn.output("response").send(answer)
