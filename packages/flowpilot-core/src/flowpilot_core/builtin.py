"""Built-in generic nodes that real graphs need regardless of providers."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from flowpilot_sdk import (
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    Response,
    TextDelta,
    Transcript,
    TurnSession,
    Utterance,
    node_translations,
)

from .registry import NodeRegistry

UTTERANCE = "text.utterance/1"
TRANSCRIPT = "text.transcript/1"
DELTA = "text.delta/1"
RESPONSE = "agent.response/1"


class TextFirst:
    """Forward the first present utterance: ``primary`` (e.g. speech) else ``fallback`` (text)."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="text.first",
        translations=node_translations(__package__, "text.first"),
        version="1.0.0",
        title="First utterance",
        category="logic",
        inputs={
            "primary": PortSpec(
                UTTERANCE,
                kind="value",
                required=False,
                description="Preferred source, usually the speech-to-text result.",
            ),
            "fallback": PortSpec(
                UTTERANCE,
                kind="value",
                required=False,
                description="Used when the primary input is absent, usually the typed text.",
            ),
        },
        outputs={
            "value": PortSpec(
                UTTERANCE, kind="value", description="The utterance that was present."
            )
        },
        description=(
            "Joins voice and text turns: one wire per input, the first value that is present wins."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        value = await turn.input("primary").value()
        if value is None:
            value = await turn.input("fallback").value()
        if value is not None:
            await turn.output("value").send(value)

    async def stop(self) -> None:
        return None


class TextConvert:
    """The same text as any other text type: connect an input, use the outputs you need.

    Several inputs may be connected when only one carries text in a turn (voice or typed text);
    if two do, the first in input order is used and the trace says which were ignored.

    Streams pass through as they come (a text stream stays a stream, partial results stay
    partial); the whole text goes out on the other outputs once it is complete. Empty text gives
    only the final transcript.
    """

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="text.convert",
        translations=node_translations(__package__, "text.convert"),
        version="1.0.0",
        title="Text converter",
        category="logic",
        inputs={
            "transcript": PortSpec(
                TRANSCRIPT,
                required=False,
                description="Recognized speech: partials pass through, the final text counts.",
            ),
            "utterance": PortSpec(
                UTTERANCE, kind="value", required=False, description="An utterance."
            ),
            "delta": PortSpec(
                DELTA, required=False, description="A text stream, e.g. an agent's answer."
            ),
            "response": PortSpec(
                RESPONSE, kind="value", required=False, description="An agent's answer."
            ),
        },
        outputs={
            "transcript": PortSpec(TRANSCRIPT, description="The text as a final transcript."),
            "utterance": PortSpec(
                UTTERANCE, kind="value", description="The text as an utterance, e.g. for an agent."
            ),
            "delta": PortSpec(DELTA, description="The text as a stream, e.g. for text-to-speech."),
            "response": PortSpec(
                RESPONSE, kind="value", description="The text as an answer, e.g. for Client output."
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "spoken": {
                    "title": "Speak the speech text",
                    "description": (
                        "From an answer, the text stream carries its speech text when the agent "
                        "gave one (e.g. numbers spelled out); on when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=120.0,
        description=(
            "Turns one kind of text into the others: recognized speech, an utterance, a text "
            "stream or an agent's answer. Every output carries the text of the input that has "
            "it in the turn."
        ),
    )

    def __init__(self) -> None:
        self._spoken = True

    async def start(self, ctx: NodeContext) -> None:
        self._spoken = bool(ctx.config.get("spoken", True))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        names = [name for name in self.spec.inputs if turn.input(name).connected]
        headers = await asyncio.gather(*(turn.input(name).header() for name in names))
        present = [name for name, header in zip(names, headers, strict=True) if header]
        if not present:
            return  # no text in this turn: the outputs are absent too
        name = present[0]  # inputs in their order: recognized speech first
        if len(present) > 1:
            turn.trace("text.convert.ignored", used=name, ignored=",".join(present[1:]))
        source: InputStream[Any] = turn.input(name)
        language = turn.context.language
        answer: Response | None = None
        said: Utterance | None = None
        if name in ("delta", "transcript"):
            # a stream passes through as it comes; the whole text follows on the other outputs
            out = turn.output(name)
            pieces: list[str] = []
            async for item in source:
                if name == "delta":
                    pieces.append(item.text)
                else:
                    pieces = [item.text]  # each transcript is the whole text so far
                    language = item.language or language
                await out.send(item)
            await out.end()
            text = "".join(pieces).strip()
        else:
            value = await source.value()
            if value is None:
                return
            text, language = value.text.strip(), value.language or language
            answer = value if isinstance(value, Response) else None
            said = value if isinstance(value, Utterance) else None
        if name != "transcript":
            transcript = turn.output("transcript")
            await transcript.send(Transcript(text, final=True, language=language))
            await transcript.end()
        if not text:
            return
        if name != "delta":
            spoken = answer.spoken if answer is not None and self._spoken else text
            delta = turn.output("delta")
            await delta.send(TextDelta(spoken))
            await delta.end()
        await turn.output("utterance").send(said or Utterance(text, language))
        await turn.output("response").send(answer or Response(text=text, language=language))


class TurnFilter:
    """Sends a text stream to one of two outputs: spoken turns or typed turns.

    Typical use: between an agent and text-to-speech, so a typed turn (chat, Telegram through
    Assist) gets its answer as text without synthesizing speech nobody hears; the typed output can
    lead somewhere else (or nowhere).
    """

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="logic.turn_filter",
        translations=node_translations(__package__, "logic.turn_filter"),
        version="2.0.0",
        title="Voice or text turn",
        category="logic",
        inputs={"text": PortSpec(DELTA, description="A text stream, e.g. an agent's answer.")},
        outputs={
            "spoken": PortSpec(
                DELTA,
                description=(
                    "The stream in spoken turns: the client sent audio (a satellite, Assist "
                    "voice); absent in typed turns."
                ),
            ),
            "typed": PortSpec(
                DELTA,
                description=(
                    "The stream in typed turns: text only (Assist chat, automations, Telegram "
                    "through Assist); absent in spoken turns."
                ),
            ),
        },
        turn_timeout_s=120.0,
        description=(
            "Sends a text stream on by the kind of turn: spoken turns to one output, typed turns "
            "to the other. Put it in front of text-to-speech so typed turns get no speech."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        source: InputStream[Any] = turn.input("text")
        if await source.header() is None:
            return
        out = turn.output("spoken" if turn.context.voice else "typed")  # the other stays absent
        async for piece in source:
            await out.send(piece)
        await out.end()


def register_builtin(registry: NodeRegistry) -> NodeRegistry:
    registry.register(TextFirst)
    registry.register(TextConvert)
    registry.register(TurnFilter)
    return registry
