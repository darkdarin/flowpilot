"""Sound signal: a short sound instead of words, e.g. "done" after a quick command.

Anything on the input plays the sound on ``speech``: Home Assistant's own confirmation sound, or
a short sound of your own stored in the node's settings (as a WAV data URL the editor makes from
an uploaded file). Join it with text-to-speech's speech using First present.
"""

from __future__ import annotations

import base64
import io
import wave
from importlib import resources
from typing import ClassVar

from flowpilot_sdk import (
    TEXT,
    AudioChunk,
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    TurnSession,
    node_translations,
)

TYPE = "audio.signal"
WAV_URL = "data:audio/wav;base64,"
# A signal, not a song: what the editor lets you upload, and what a graph may carry
MAX_SECONDS = 5.0
CHUNK_S = 0.1


def read_wav(data: bytes) -> tuple[AudioFormat, bytes]:
    """16-bit PCM and its format from WAV bytes; ``ValueError`` for anything else."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getsampwidth() != 2:
                raise ValueError("the sound must be 16-bit PCM")
            fmt = AudioFormat(sample_rate=wav.getframerate(), channels=wav.getnchannels())
            pcm = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as err:
        raise ValueError(f"not a WAV sound: {err}") from err
    if len(pcm) / (fmt.sample_rate * fmt.bytes_per_sample) > MAX_SECONDS:
        raise ValueError(f"the sound is longer than {MAX_SECONDS:g} s")
    return fmt, pcm


def default_sound() -> tuple[AudioFormat, bytes]:
    """Home Assistant's acknowledge sound (see NOTICE)."""
    data = resources.files(__package__).joinpath("sounds/acknowledge.wav").read_bytes()
    return read_wav(data)


class SoundSignal:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Sound signal",
        category="speech",
        inputs={
            "trigger": PortSpec(
                TEXT,
                description="Anything here plays the sound, e.g. Quick commands' confirmation.",
            ),
        },
        outputs={
            "speech": PortSpec(
                "audio.pcm/1", description="The sound, e.g. for Client output's speech."
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "sound": {
                    "title": "Sound",
                    "description": (
                        f"A short sound of your own, up to {MAX_SECONDS:g} s; Home Assistant's "
                        "confirmation sound when not set."
                    ),
                    "type": "string",
                    "format": "audio",
                    "maxLength": 400_000,
                },
                "voice_only": {
                    "title": "Only in spoken turns",
                    "description": "Typed turns get no sound; on when not set.",
                    "type": "boolean",
                    "default": True,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=30.0,
        description=(
            "Plays a short sound when anything comes to its input: a confirmation instead of "
            "words after a quick command. Home Assistant's own sound, or upload yours."
        ),
    )

    def __init__(self) -> None:
        self._sound: tuple[AudioFormat, bytes] = (AudioFormat(sample_rate=16000), b"")
        self._voice_only = True

    async def start(self, ctx: NodeContext) -> None:
        own = ctx.config.get("sound")
        if isinstance(own, str) and own.startswith(WAV_URL):
            self._sound = read_wav(base64.b64decode(own[len(WAV_URL) :]))
        else:
            self._sound = default_sound()
        self._voice_only = bool(ctx.config.get("voice_only", True))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        trigger = turn.input("trigger")
        header = await trigger.header()
        trigger.close()  # what came does not matter, only that it came
        if header is None or (self._voice_only and not turn.context.voice):
            return
        fmt, pcm = self._sound
        out = turn.output("speech")
        await out.open(fmt.as_meta())
        frame = fmt.bytes_per_sample
        step = max(frame, int(fmt.sample_rate * CHUNK_S) * frame)
        for offset in range(0, len(pcm), step):
            await out.send(AudioChunk(pcm[offset : offset + step], offset // frame))
        await out.end()
