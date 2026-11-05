"""``vad.segmenter``: pass audio through and end the stream when the voice command is over."""

from __future__ import annotations

from typing import Any, ClassVar

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    AudioFormat,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    TurnSession,
    node_translations,
)

from .segmenter import (
    FRAME_S,
    CommandSegmenter,
    Detector,
    EnergyDetector,
    FrameBuffer,
    MicroDetector,
    Outcome,
)


class VadSegmenter:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="vad.segmenter",
        translations=node_translations(__package__, "vad.segmenter"),
        version="1.0.0",
        title="Voice command segmenter",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1", description="Speech from the microphone.")},
        outputs={
            "audio": PortSpec(
                "audio.pcm/1", description="The same audio, ending when the command ends."
            )
        },
        config_schema={
            "type": "object",
            "properties": {
                "engine": {
                    "title": "Detector",
                    "description": (
                        "micro: the speech detector Assist uses (pymicro-vad); energy: a loudness "
                        "threshold."
                    ),
                    "enum": ["micro", "energy"],
                    ENUM_TITLES_KEY: {
                        "micro": "Speech detector (micro)",
                        "energy": "Loudness threshold (energy)",
                    },
                },
                "speech_s": {
                    "title": "Speech to start, s",
                    "description": (
                        "Speech needed before a command counts as started; 0.3 when not set."
                    ),
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "command_s": {
                    "title": "Shortest command, s",
                    "description": "A command is never shorter than this; 1.0 when not set.",
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "silence_s": {
                    "title": "Silence to end, s",
                    "description": "Silence that ends a started command; 0.7 when not set.",
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "timeout_s": {
                    "title": "Longest command, s",
                    "description": (
                        "The audio ends after this even if the user keeps talking; 15 when not set."
                    ),
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "speech_dbfs": {
                    "title": "Speech level, dBFS",
                    "description": (
                        "For the energy detector: loudness that counts as speech; -35 when not set."
                    ),
                    "type": "number",
                    "maximum": 0,
                },
            },
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description=(
            "Passes the audio through without delay and ends it when the voice command is over, "
            "the way Assist does. Put it in front of speech-to-text that does not detect the end "
            "of speech itself (Whisper, most Home Assistant STT entities)."
        ),
    )

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        self._detector()  # fail at start if the engine is unavailable

    async def stop(self) -> None:
        return None

    def _detector(self) -> Detector:
        if self._config.get("engine", "micro") == "energy":
            return EnergyDetector(speech_dbfs=float(self._config.get("speech_dbfs", -35.0)))
        return MicroDetector()

    def _segmenter(self) -> CommandSegmenter:
        defaults = CommandSegmenter()
        return CommandSegmenter(
            speech_s=float(self._config.get("speech_s", defaults.speech_s)),
            command_s=float(self._config.get("command_s", defaults.command_s)),
            silence_s=float(self._config.get("silence_s", defaults.silence_s)),
            timeout_s=float(self._config.get("timeout_s", defaults.timeout_s)),
        )

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return
        fmt = AudioFormat.from_meta(header.meta)
        if (fmt.sample_rate, fmt.channels, fmt.encoding) != (16000, 1, "s16le"):
            raise ValueError(f"vad.segmenter needs 16 kHz mono s16le audio, got {fmt}")

        out = turn.output("audio")
        await out.open(header.meta)
        detector, segmenter, buffer = self._detector(), self._segmenter(), FrameBuffer()
        speech_started = False
        async for chunk in audio:
            await out.send(chunk)  # forward first: the speech-to-text node never waits on VAD
            for frame in buffer.frames(chunk.pcm):
                outcome = segmenter.process(FRAME_S, detector.probability(frame))
                if segmenter.in_command and not speech_started:
                    speech_started = True
                    turn.trace("vad.speech_start", offset=chunk.sample_offset)
                if outcome is not Outcome.CONTINUE:
                    turn.trace(f"vad.{outcome.value}", offset=chunk.sample_offset)
                    audio.close()
                    await out.end()
                    return
