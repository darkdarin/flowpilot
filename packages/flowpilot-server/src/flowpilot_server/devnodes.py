"""Development-only nodes for wiring tests without real providers. Enabled with ``--dev``."""

from __future__ import annotations

from typing import ClassVar

from flowpilot_sdk import (
    NodeContext,
    NodeSpec,
    PortSpec,
    Transcript,
    TurnSession,
    Utterance,
)


class FixedStt:
    """Pretends to recognize a configured phrase once the audio stream ends."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="dev.fixed_stt",
        version="1.0.0",
        title="Fixed phrase STT (dev)",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1")},
        outputs={
            "transcript": PortSpec("text.transcript/1"),
            "utterance": PortSpec("text.utterance/1", kind="value"),
        },
        config_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_chunks": {"type": "integer", "minimum": 1},
            },
            "required": ["text"],
        },
        turn_timeout_s=30.0,
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


DEV_NODES = [FixedStt]
