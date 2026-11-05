"""Development-only nodes for wiring tests without real providers. Enabled with ``--dev``."""

from __future__ import annotations

from typing import ClassVar

from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    Node,
    NodeContext,
    NodeSpec,
    PortSpec,
    Transcript,
    TurnSession,
    Utterance,
    node_translations,
)


class FixedStt:
    """Pretends to recognize a configured phrase once the audio stream ends."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="dev.fixed_stt",
        translations=node_translations(__package__, "dev.fixed_stt"),
        version="1.0.0",
        title="Fixed phrase STT (dev)",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1", description="Audio; read and ignored.")},
        outputs={
            "transcript": PortSpec(
                "text.transcript/1", description="The configured phrase as the final text."
            ),
            "utterance": PortSpec(
                "text.utterance/1",
                kind="value",
                description="The configured phrase as an utterance.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "text": {
                    "title": "Phrase",
                    "description": "What the node recognizes.",
                    "type": "string",
                },
                "max_chunks": {
                    "title": "Stop after chunks",
                    "description": (
                        "End the audio after this many chunks, as if speech ended; read to the "
                        "end when not set."
                    ),
                    "type": "integer",
                    "minimum": 1,
                },
            },
            "required": ["text"],
        },
        turn_timeout_s=30.0,
        description=(
            "For testing without a speech service: reads the audio and always recognizes the "
            "configured phrase."
        ),
    )

    def __init__(self) -> None:
        self._text = ""
        self._max_chunks: int | None = None

    async def start(self, ctx: NodeContext) -> None:
        self._text = str(ctx.config["text"])
        self._max_chunks = ctx.config.get("max_chunks")

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.input("audio")
        if await audio.header() is None:
            return
        n = 0
        async for _ in audio:
            n += 1
            if self._max_chunks is not None and n >= self._max_chunks:
                audio.close()  # acts as end-of-speech detection
                break
        turn.trace("dev.fixed_stt", chunks=n)
        await turn.output("transcript").send(Transcript(self._text, final=True))
        await turn.output("utterance").send(Utterance(self._text, turn.context.language))

    async def stop(self) -> None:
        return None


class BeepTts:
    """Pretends to speak: a fixed tone per character, so tests can tell the speech of a turn."""

    RATE = 16000
    SAMPLES_PER_CHAR = 160  # 10 ms
    LEVEL = 1000

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="dev.beep_tts",
        translations=node_translations(__package__, "dev.beep_tts"),
        version="1.0.0",
        title="Beeping TTS (dev)",
        category="speech",
        inputs={"text": PortSpec("text.delta/1", description="Text to speak.")},
        outputs={
            "speech": PortSpec(
                "audio.pcm/1", description="10 ms of a constant tone per character, 16 kHz."
            )
        },
        turn_timeout_s=30.0,
        description=(
            "For testing without a speech service: speaks every character as 10 ms of a tone, "
            "so the length of the speech tells which text it was."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        text = turn.input("text")
        if await text.header() is None:
            return
        speech = turn.output("speech")
        await speech.open(AudioFormat(sample_rate=self.RATE).as_meta())
        offset = 0
        sample = self.LEVEL.to_bytes(2, "little", signed=True)
        async for piece in text:
            samples = len(piece.text) * self.SAMPLES_PER_CHAR
            if samples:
                await speech.send(AudioChunk(sample * samples, offset))
                offset += samples


DEV_NODES: list[type[Node]] = [FixedStt, BeepTts]
