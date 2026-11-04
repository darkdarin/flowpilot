"""Client adapter nodes and Home Assistant nodes served over host operations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from flowpilot_sdk import (
    OPTIONS_KEY,
    AudioChunk,
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    Response,
    StreamError,
    TextDelta,
    Transcript,
    TurnSession,
    Utterance,
)
from flowpilot_sdk.helpers import SpeechWriter, split_sentences

AUDIO = "audio.pcm/1"
UTTERANCE = "text.utterance/1"
HA_CONVERSATION_OP = "ha.conversation.process"
HA_STT_OP = "ha.stt.process"
HA_TTS_OP = "ha.tts.synthesize"


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
            "properties": {
                "agent_id": {"type": "string", "minLength": 1, OPTIONS_KEY: "ha.conversation"}
            },
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


class HaStt(_Lifecycle):
    """Speech-to-text by any STT entity configured in Home Assistant (via the client).

    Home Assistant STT entities answer once the audio ends, so put ``vad.segmenter`` in front of
    this node unless the source ends the audio itself.
    """

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="ha.stt",
        version="1.0.0",
        title="Home Assistant speech-to-text",
        category="speech",
        inputs={"audio": PortSpec(AUDIO)},
        outputs={
            "transcript": PortSpec("text.transcript/1"),
            "utterance": PortSpec(UTTERANCE, kind="value"),
        },
        config_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "pattern": "^stt\\.", OPTIONS_KEY: "ha.stt"},
                "language": {"type": "string"},
            },
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        host_ops=frozenset({HA_STT_OP}),
        turn_timeout_s=60.0,
    )

    def __init__(self) -> None:
        self._entity_id = ""
        self._language: str | None = None

    async def start(self, ctx: NodeContext) -> None:
        self._entity_id = str(ctx.config["entity_id"])
        self._language = ctx.config.get("language")

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.input("audio")
        header = await audio.header()
        if header is None:
            return
        language = self._language or turn.context.language

        async def pcm() -> AsyncIterator[bytes]:
            async for chunk in audio:
                yield chunk.pcm

        result = await turn.call_host(
            HA_STT_OP,
            {"entity_id": self._entity_id, "language": language},
            audio_in=pcm(),
            audio_format=AudioFormat.from_meta(header.meta),
        )
        text = str(result.get("text") or "")
        await turn.output("transcript").send(Transcript(text, final=True, language=language))
        if text.strip():
            await turn.output("utterance").send(Utterance(text, language))


class HaTts(_Lifecycle):
    """Text-to-speech by any TTS entity in Home Assistant, sentence by sentence (via the client)."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="ha.tts",
        version="1.0.0",
        title="Home Assistant text-to-speech",
        category="speech",
        inputs={"text": PortSpec("text.delta/1")},
        outputs={"speech": PortSpec(AUDIO)},
        config_schema={
            "type": "object",
            "properties": {
                "engine": {"type": "string", "pattern": "^tts\\.", OPTIONS_KEY: "ha.tts"},
                "language": {"type": "string"},
                "voice": {"type": "string"},
                "sample_rate": {"type": "integer", "minimum": 8000},
            },
            "required": ["engine"],
            "additionalProperties": False,
        },
        host_ops=frozenset({HA_TTS_OP}),
        turn_timeout_s=60.0,
    )

    def __init__(self) -> None:
        self._args: dict[str, Any] = {}

    async def start(self, ctx: NodeContext) -> None:
        cfg = ctx.config
        self._args = {
            "engine": str(cfg["engine"]),
            "language": cfg.get("language"),
            "voice": cfg.get("voice"),
            "sample_rate": int(cfg.get("sample_rate", 16000)),
        }

    async def run_turn(self, turn: TurnSession) -> None:
        text = turn.input("text")
        if await text.header() is None:
            return
        writer = SpeechWriter(turn.output("speech"))
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        worker = asyncio.create_task(self._speak(turn, queue, writer))
        try:
            buffer = ""
            async for delta in text:
                buffer += delta.text
                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    queue.put_nowait(sentence)
            if buffer.strip():
                queue.put_nowait(buffer.strip())
            queue.put_nowait(None)
            await worker
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def _speak(
        self, turn: TurnSession, queue: asyncio.Queue[str | None], writer: SpeechWriter
    ) -> None:
        while (sentence := await queue.get()) is not None:
            args = {**self._args, "text": sentence}
            if args["language"] is None:
                args["language"] = turn.context.language
            async with turn.stream_host(HA_TTS_OP, args) as audio:
                if audio.format is not None:
                    await writer.start(audio.format)
                    async for pcm in audio:
                        await writer.chunk(pcm)
                await audio.result()
