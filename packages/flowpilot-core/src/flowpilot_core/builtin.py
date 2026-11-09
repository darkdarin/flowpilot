"""Built-in generic nodes that real graphs need regardless of providers."""

from __future__ import annotations

from typing import Any, ClassVar

from flowpilot_sdk import (
    TEXT,
    TEXT_OR_AUDIO,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    Response,
    SettingValue,
    StreamError,
    TextDelta,
    Transcript,
    TurnSession,
    Utterance,
    node_translations,
    type_kind,
)

from .registry import NodeRegistry

UTTERANCE = "text.utterance/1"
TRANSCRIPT = "text.transcript/1"
DELTA = "text.delta/1"
RESPONSE = "agent.response/1"


class TextFirst:
    """Pass on the first present text: ``primary`` (e.g. speech, a quick command's answer) else
    ``fallback`` (typed text, an agent's answer). Any kind of text, the same on both inputs and
    the output; a stream passes through as it comes."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="text.first",
        translations=node_translations(__package__, "text.first"),
        version="1.2.0",
        title="First present",
        category="logic",
        inputs={
            "primary": PortSpec(
                TEXT_OR_AUDIO,
                required=False,
                var="T",
                description="Preferred source, e.g. the speech-to-text result.",
            ),
            "fallback": PortSpec(
                TEXT_OR_AUDIO,
                required=False,
                var="T",
                description="Used when the primary input is absent, e.g. the typed text.",
            ),
        },
        outputs={
            "value": PortSpec(
                TEXT_OR_AUDIO,
                var="T",
                description="What was present, of the type connected to the inputs.",
            )
        },
        description=(
            "Joins two branches of a graph, such as voice and typed turns or a quick command "
            "and an agent: one wire per input, any kind of text (the output has the type of the "
            "inputs); the primary one wins when it is present, a stream passes on as it comes. "
            "Audio too, e.g. a confirmation sound or the speech of text-to-speech."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        source = turn.input("primary")
        try:
            header = await source.header()
        except StreamError:
            header = None
        if header is None:
            source = turn.input("fallback")
            try:
                header = await source.header()
            except StreamError:
                return
        if header is None:
            return
        out = turn.output("value")
        if type_kind(header.type) == "value":
            value = await source.value()
            if value is not None:
                await out.send(value)
            return
        await out.open(header.meta, type_id=header.type)  # e.g. the audio format
        async for item in source:
            await out.send(item)
        await out.end()

    async def stop(self) -> None:
        return None


# the output of each text type: the input passes through on it
_OUTPUT_OF = {
    TRANSCRIPT: "transcript",
    UTTERANCE: "utterance",
    DELTA: "delta",
    RESPONSE: "response",
}


class TextConvert:
    """The same text as any other text type: connect any text to the input, use the outputs
    you need (the one of the input's own type passes it through).

    Streams pass through as they come (a text stream stays a stream, partial results stay
    partial); the whole text goes out on the other outputs once it is complete. Empty text gives
    only the final transcript.
    """

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="text.convert",
        translations=node_translations(__package__, "text.convert"),
        version="2.0.0",
        title="Text converter",
        category="logic",
        inputs={
            "text": PortSpec(
                TEXT,
                description=(
                    "Any text: recognized speech (partials pass through, the final text counts), "
                    "an utterance, a text stream or an agent's answer."
                ),
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
            "stream or an agent's answer. Connect any of them; every output carries its text."
        ),
    )

    def __init__(self) -> None:
        self._spoken = True

    async def start(self, ctx: NodeContext) -> None:
        self._spoken = bool(ctx.config.get("spoken", True))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        source: InputStream[Any] = turn.input("text")
        header = await source.header()
        if header is None:
            return  # no text in this turn: the outputs are absent too
        name = _OUTPUT_OF[header.type]
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


class Value:
    """A fixed value for a setting another node takes as an input: a voice, a speed, instructions.
    Through a Condition it sets the node one way in some turns and another way in others."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="logic.value",
        translations=node_translations(__package__, "logic.value"),
        version="1.0.0",
        title="Value",
        category="logic",
        outputs={
            "value": PortSpec(
                "config.value/1",
                kind="value",
                description="The value, every turn: wire it to a setting made an input.",
            )
        },
        config_schema={
            "type": "object",
            "properties": {
                "value": {
                    "title": "Value",
                    "description": (
                        "Text, a number or yes/no; it takes the type of the setting it goes to "
                        '("0.8" for a speed, "yes" for a switch).'
                    ),
                    "type": "string",
                    "format": "multiline",
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        description=(
            "A fixed value for a setting another node takes as an input (a voice, a speed, "
            "instructions); with a Condition, one value in some turns and another in others."
        ),
    )

    def __init__(self) -> None:
        self._value = ""

    async def start(self, ctx: NodeContext) -> None:
        self._value = str(ctx.config.get("value", ""))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        await turn.output("value").send(SettingValue(self._value))


def register_builtin(registry: NodeRegistry) -> NodeRegistry:
    registry.register(TextFirst)
    registry.register(TextConvert)
    registry.register(TurnFilter)
    registry.register(Value)
    return registry
