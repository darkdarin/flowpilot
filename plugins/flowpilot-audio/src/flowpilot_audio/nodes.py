"""``audio.gain`` and ``audio.agc``: make the incoming speech louder before speech-to-text.

Both pass the audio through chunk by chunk (no added delay) and report the levels they saw in the
trace, so live debug shows whether the microphone is too quiet: ``in_rms_dbfs`` is the speech
level that came in, ``clipped`` how many samples hit the 16-bit limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, ClassVar

from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    TurnSession,
    node_translations,
)

from .pcm import apply_gain, db_to_gain, dbfs, peak, rms, samples, to_bytes

AUDIO = "audio.pcm/1"


def _check_format(node: str, meta: dict[str, Any]) -> AudioFormat:
    fmt = AudioFormat.from_meta(meta)
    if fmt.encoding != "s16le":
        raise ValueError(f"{node} needs 16-bit PCM (s16le), got {fmt.encoding}")
    return fmt


@dataclass(slots=True)
class Levels:
    """What a turn's audio looked like, for the trace."""

    speech_power: float = 0.0  # sum of squares of blocks above the noise floor
    speech_samples: int = 0
    in_peak: int = 0
    out_peak: int = 0
    clipped: int = 0
    total: int = 0

    def attrs(self) -> dict[str, Any]:
        speech = math.sqrt(self.speech_power / self.speech_samples) if self.speech_samples else 0
        return {
            "in_rms_dbfs": round(dbfs(speech), 1),
            "in_peak_dbfs": round(dbfs(self.in_peak), 1),
            "out_peak_dbfs": round(dbfs(self.out_peak), 1),
            "clipped": self.clipped,
            "clipped_pct": round(100 * self.clipped / self.total, 3) if self.total else 0.0,
        }


class Gain:
    """A fixed gain."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="audio.gain",
        translations=node_translations(__package__, "audio.gain"),
        version="1.0.0",
        title="Gain",
        category="speech",
        inputs={"audio": PortSpec(AUDIO, description="Audio to make louder or quieter.")},
        outputs={"audio": PortSpec(AUDIO, description="The same audio with the gain applied.")},
        config_schema={
            "type": "object",
            "properties": {
                "gain_db": {
                    "title": "Gain, dB",
                    "description": (
                        "+6 dB doubles the amplitude, -6 dB halves it; 6 when not set. Loud "
                        "parts that would exceed full scale are clipped: see clipped in the trace."
                    ),
                    "type": "number",
                    "default": 6,
                    "minimum": -30,
                    "maximum": 40,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description=(
            "Makes the audio louder (or quieter) by a fixed amount, without delay. For a "
            "microphone that is always too quiet; for speech that varies, use Automatic gain."
        ),
    )

    def __init__(self) -> None:
        self._gain = db_to_gain(6.0)

    async def start(self, ctx: NodeContext) -> None:
        self._gain = db_to_gain(float(ctx.config.get("gain_db", 6.0)))

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return
        _check_format(self.spec.type, dict(header.meta))
        out = turn.output("audio")
        await out.open(header.meta)
        levels = Levels()
        async for chunk in audio:
            values = samples(chunk.pcm)
            result, clipped = apply_gain(values, self._gain)
            levels.speech_power += math.fsum(v * v for v in values)
            levels.speech_samples += len(values)
            levels.in_peak = max(levels.in_peak, peak(values))
            levels.out_peak = max(levels.out_peak, peak(result))
            levels.clipped += clipped
            levels.total += len(values)
            await out.send(AudioChunk(to_bytes(result), chunk.sample_offset))
        await out.end()
        turn.trace("audio.gain", **levels.attrs())


@dataclass(slots=True)
class GainControl:
    """Automatic gain control on 10 ms blocks.

    Blocks louder than the noise floor pull the gain towards ``target - level`` (within
    ``-max_cut_db .. max_gain_db``): down quickly (attack), up slowly (release). The first speech
    block sets the gain at once, so the start of a command is not lost while the gain ramps up.
    Quieter blocks keep the gain, so pauses and background noise are not pumped up. A peak limiter
    lowers the gain of any block that would go above the ceiling, so nothing clips.
    """

    target_dbfs: float = -20.0
    max_gain_db: float = 24.0
    max_cut_db: float = 12.0
    noise_floor_dbfs: float = -50.0
    attack_ms: float = 20.0
    release_ms: float = 500.0
    ceiling_dbfs: float = -1.0
    gain_db: float | None = None  # None until the first block of speech
    applied: float = 1.0  # linear gain at the end of the last block
    min_gain_db: float = math.inf
    max_seen_gain_db: float = -math.inf
    limited: int = 0  # blocks the limiter lowered
    levels: Levels = field(default_factory=Levels)

    def process(self, values: Any, block_ms: float) -> Any:
        level_rms, level_peak = rms(values), peak(values)
        level = dbfs(level_rms)
        if level >= self.noise_floor_dbfs:
            wanted = min(self.max_gain_db, max(-self.max_cut_db, self.target_dbfs - level))
            if self.gain_db is None:
                self.gain_db = wanted
            else:
                time_ms = self.attack_ms if wanted < self.gain_db else self.release_ms
                self.gain_db += (wanted - self.gain_db) * (1 - math.exp(-block_ms / time_ms))
            self.levels.speech_power += level_rms * level_rms * len(values)
            self.levels.speech_samples += len(values)
        gain = db_to_gain(self.gain_db or 0.0)
        ceiling = db_to_gain(self.ceiling_dbfs) * 32767
        if level_peak * gain > ceiling:
            gain = ceiling / level_peak
            self.limited += 1
        result, clipped = apply_gain(values, self.applied, gain)
        if result and peak(result) > ceiling + 1:
            # the ramp from a louder previous gain overshot: apply the limited gain flat
            result, clipped = apply_gain(values, gain)
        self.applied = gain
        applied_db = 20 * math.log10(gain)
        self.min_gain_db = min(self.min_gain_db, applied_db)
        self.max_seen_gain_db = max(self.max_seen_gain_db, applied_db)
        self.levels.in_peak = max(self.levels.in_peak, level_peak)
        self.levels.out_peak = max(self.levels.out_peak, peak(result))
        self.levels.clipped += clipped
        self.levels.total += len(values)
        return result

    def attrs(self) -> dict[str, Any]:
        found = self.min_gain_db != math.inf
        return self.levels.attrs() | {
            "gain_db": round(20 * math.log10(self.applied), 1),
            "min_gain_db": round(self.min_gain_db, 1) if found else None,
            "max_gain_db": round(self.max_seen_gain_db, 1) if found else None,
            "limited_blocks": self.limited,
            "speech": self.gain_db is not None,
        }


class AutoGain:
    """Automatic gain control (normalization) for speech."""

    BLOCK_MS = 10

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="audio.agc",
        translations=node_translations(__package__, "audio.agc"),
        version="1.0.0",
        title="Automatic gain",
        category="speech",
        inputs={"audio": PortSpec(AUDIO, description="Speech from the microphone.")},
        outputs={
            "audio": PortSpec(AUDIO, description="The same speech, brought to an even level.")
        },
        config_schema={
            "type": "object",
            "properties": {
                "target_dbfs": {
                    "title": "Target level, dBFS",
                    "description": "Speech loudness (RMS) to reach; -20 when not set.",
                    "type": "number",
                    "default": -20,
                    "minimum": -40,
                    "maximum": -3,
                },
                "max_gain_db": {
                    "title": "Most gain, dB",
                    "description": "Quiet speech is raised at most by this; 24 when not set.",
                    "type": "number",
                    "default": 24,
                    "minimum": 0,
                    "maximum": 50,
                },
                "max_cut_db": {
                    "title": "Most cut, dB",
                    "description": "Loud speech is lowered at most by this; 12 when not set.",
                    "type": "number",
                    "default": 12,
                    "minimum": 0,
                    "maximum": 40,
                },
                "noise_floor_dbfs": {
                    "title": "Noise floor, dBFS",
                    "description": (
                        "Anything quieter is treated as background: it does not change the gain, "
                        "so pauses and noise are not raised; -50 when not set."
                    ),
                    "type": "number",
                    "default": -50,
                    "minimum": -90,
                    "maximum": -10,
                },
                "attack_ms": {
                    "title": "Attack, ms",
                    "description": "How fast the gain goes down for loud speech; 20 when not set.",
                    "type": "number",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 2000,
                },
                "release_ms": {
                    "title": "Release, ms",
                    "description": "How fast the gain goes up for quiet speech; 500 when not set.",
                    "type": "number",
                    "default": 500,
                    "minimum": 1,
                    "maximum": 10000,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description=(
            "Brings speech to an even level without delay: raises quiet speech, lowers loud "
            "speech, leaves background noise alone and never clips. Put it in front of the "
            "voice command segmenter and speech-to-text."
        ),
    )

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)

    async def stop(self) -> None:
        return None

    def control(self) -> GainControl:
        """A fresh gain control for one turn (no state carries over between turns)."""
        cfg, d = self._config, GainControl()
        return GainControl(
            target_dbfs=float(cfg.get("target_dbfs", d.target_dbfs)),
            max_gain_db=float(cfg.get("max_gain_db", d.max_gain_db)),
            max_cut_db=float(cfg.get("max_cut_db", d.max_cut_db)),
            noise_floor_dbfs=float(cfg.get("noise_floor_dbfs", d.noise_floor_dbfs)),
            attack_ms=float(cfg.get("attack_ms", d.attack_ms)),
            release_ms=float(cfg.get("release_ms", d.release_ms)),
        )

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return
        fmt = _check_format(self.spec.type, dict(header.meta))
        block = fmt.sample_rate * fmt.channels * self.BLOCK_MS // 1000
        out = turn.output("audio")
        await out.open(header.meta)
        control = self.control()
        async for chunk in audio:
            values = samples(chunk.pcm)
            result = bytearray()
            for i in range(0, len(values), block):
                part = values[i : i + block]
                block_ms = self.BLOCK_MS * len(part) / block
                result += to_bytes(control.process(part, block_ms))
            await out.send(AudioChunk(bytes(result), chunk.sample_offset))
        await out.end()
        turn.trace("audio.agc", **control.attrs())
