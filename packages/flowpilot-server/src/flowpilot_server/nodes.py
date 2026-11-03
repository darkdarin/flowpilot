"""Client adapter nodes and Home Assistant nodes served over host operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    Response,
    StreamError,
    TextDelta,
    TurnSession,
    Utterance,
)

AUDIO = "audio.pcm/1"
UTTERANCE = "text.utterance/1"
HA_CONVERSATION_OP = "ha.conversation.process"


@dataclass
class ClientInput:
    """What the client sends for one turn: an utterance, an audio stream, or both."""

    text: str | None = None
    audio_format: AudioFormat | None = None
    audio: asyncio.Queue[bytes | None] | None = None


class ClientOutlet(Protocol):
    """Where a turn's results go back to the client."""

    async def transcript(self, text: str, final: bool) -> None: ...

    async def delta(self, text: str) -> None: ...

    async def response(self, response: Response) -> None: ...

    async def speech_start(self, fmt: AudioFormat) -> None: ...

    async def speech_chunk(self, pcm: bytes) -> None: ...

    async def speech_end(self) -> None: ...


class _Lifecycle:
    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None


class ClientSource(_Lifecycle):
    """Graph input: the audio and/or text the client sent for this turn."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="client.source",
        version="1.0.0",
        title="Client input",
        category="input",
        outputs={
            "audio": PortSpec(AUDIO),
            "utterance": PortSpec(UTTERANCE, kind="value"),
        },
    )

    def __init__(self) -> None:
        self.inputs: dict[str, ClientInput] = {}

    async def run_turn(self, turn: TurnSession) -> None:
        inp = self.inputs.pop(turn.context.turn_id, None)
        if inp is None:
            return
        if inp.text is not None:
            await turn.output("utterance").send(Utterance(inp.text, turn.context.language))
        if inp.audio is not None and inp.audio_format is not None:
            audio = turn.output("audio")
            await audio.open(inp.audio_format.as_meta())
            offset = 0
            while (pcm := await inp.audio.get()) is not None:
                await audio.send(AudioChunk(pcm, offset))
                offset += len(pcm) // inp.audio_format.bytes_per_sample


class ClientSink(_Lifecycle):
    """Graph output: forwards transcript, response text and speech to the client as they arrive."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="client.sink",
        version="1.0.0",
        title="Client output",
        category="output",
        inputs={
            "transcript": PortSpec("text.transcript/1", required=False),
            "delta": PortSpec("text.delta/1", required=False),
            "response": PortSpec("agent.response/1", kind="value"),
            "speech": PortSpec(AUDIO, required=False),
        },
    )

    def __init__(self) -> None:
        self.outlets: dict[str, ClientOutlet] = {}

    async def run_turn(self, turn: TurnSession) -> None:
        outlet = self.outlets.pop(turn.context.turn_id, None)
        if outlet is None:
            return

        async def transcripts() -> None:
            async for t in turn.input("transcript"):
                await outlet.transcript(t.text, t.final)

        async def deltas() -> None:
            async for d in turn.input("delta"):
                await outlet.delta(d.text)

        async def speech() -> None:
            stream = turn.input("speech")
            header = await stream.header()
            if header is None:
                return
            await outlet.speech_start(AudioFormat.from_meta(header.meta))
            try:
                async for chunk in stream:
                    await outlet.speech_chunk(chunk.pcm)
            finally:
                await outlet.speech_end()

        async def response() -> None:
            value = await turn.input("response").value()
            if value is not None:
                await outlet.response(value)

        async def guarded(coro: Any) -> None:
            # Intermediate streams may fail upstream; the rest must still reach the client.
            try:
                await coro
            except StreamError:
                return

        await asyncio.gather(
            guarded(transcripts()), guarded(deltas()), guarded(speech()), response()
        )


@dataclass
class _HaConversationConfig:
    agent_id: str | None = None


class HaConversation(_Lifecycle):
    """Agent node backed by a Home Assistant conversation agent, called via the client."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="ha.conversation",
        version="1.0.0",
        title="Home Assistant conversation agent",
        category="agent",
        inputs={
            "utterance": PortSpec(UTTERANCE, kind="value"),
            "principal": PortSpec("identity.principal/1", kind="value", required=False),
        },
        outputs={
            "delta": PortSpec("text.delta/1"),
            "speech_delta": PortSpec("text.delta/1"),
            "response": PortSpec("agent.response/1", kind="value"),
        },
        config_schema={
            "type": "object",
            "properties": {"agent_id": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
        host_ops=frozenset({HA_CONVERSATION_OP}),
        turn_timeout_s=20.0,
    )

    def __init__(self) -> None:
        self._config = _HaConversationConfig()

    async def start(self, ctx: NodeContext) -> None:
        self._config = _HaConversationConfig(agent_id=ctx.config.get("agent_id"))

    async def run_turn(self, turn: TurnSession) -> None:
        utterance: Utterance | None = await turn.input("utterance").value()
        if utterance is None:
            return
        principal: Principal | None = await turn.input("principal").value()
        ctx = turn.context
        args: dict[str, Any] = {
            "text": utterance.text,
            "language": utterance.language or ctx.language,
            "agent_id": self._config.agent_id,
            "conversation_id": ctx.external.get("ha.conversation_id"),
            "device_id": ctx.device.device_id if ctx.device else None,
            "satellite_id": ctx.device.satellite_id if ctx.device else None,
            "principal": (
                {"user_id": principal.user_id, "roles": sorted(principal.roles)}
                if principal is not None and principal.is_known
                else None
            ),
        }
        result = await turn.call_host(HA_CONVERSATION_OP, args)
        speech = str(result.get("speech", ""))
        if speech:
            await turn.output("delta").send(TextDelta(speech))
        await turn.output("response").send(
            Response(
                text=speech,
                language=args["language"],
                continue_conversation=bool(result.get("continue_conversation", False)),
                data={"conversation_id": result.get("conversation_id")},
            )
        )
