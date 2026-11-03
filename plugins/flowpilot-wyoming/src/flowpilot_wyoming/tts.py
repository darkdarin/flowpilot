"""``wyoming.tts``: text-to-speech through any Wyoming TTS service, streamed."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any, ClassVar, Literal

from wyoming.audio import AudioChunk as WyAudioChunk
from wyoming.audio import AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    InputStream,
    NodeContext,
    NodeSpec,
    OutputStream,
    PortSpec,
    TurnSession,
)

from .common import (
    DEFAULT_CONNECT_TIMEOUT_S,
    Address,
    WyomingError,
    connect,
    describe,
    raise_on_error,
)

# End of a sentence: terminal punctuation (optionally closing quotes/brackets) followed by space,
# or a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]»]*\s+|\n+")
MIN_SENTENCE_CHARS = 12


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split complete sentences off ``buffer``; return them and the incomplete rest.

    Very short sentences are merged with the next one so the TTS gets meaningful pieces.
    """
    sentences: list[str] = []
    start = 0
    pending = ""
    for match in _SENTENCE_END.finditer(buffer):
        piece = pending + buffer[start : match.end()]
        start = match.end()
        if len(piece.strip()) < MIN_SENTENCE_CHARS:
            pending = piece
            continue
        sentences.append(piece.strip())
        pending = ""
    return sentences, pending + buffer[start:]


class _SpeechWriter:
    """Opens the speech output once and keeps sample offsets across synthesis requests."""

    def __init__(self, out: OutputStream[Any]) -> None:
        self._out = out
        self._fmt: AudioFormat | None = None
        self._offset = 0

    async def start(self, event_start: AudioStart) -> None:
        if event_start.width != 2:
            raise WyomingError(f"unsupported sample width {event_start.width}, need 16-bit")
        fmt = AudioFormat(sample_rate=event_start.rate, channels=event_start.channels)
        if self._fmt is None:
            self._fmt = fmt
            await self._out.open(fmt.as_meta())
        elif fmt != self._fmt:
            raise WyomingError(f"audio format changed mid-answer: {self._fmt} -> {fmt}")

    async def chunk(self, pcm: bytes) -> None:
        if self._fmt is None:
            raise WyomingError("audio chunk before audio start")
        await self._out.send(AudioChunk(pcm, self._offset))
        self._offset += len(pcm) // self._fmt.bytes_per_sample


class WyomingTts:
    """Synthesizes streamed response text with a Wyoming TTS service."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="wyoming.tts",
        version="1.0.0",
        title="Wyoming text-to-speech",
        category="speech",
        inputs={"text": PortSpec("text.delta/1")},
        outputs={"speech": PortSpec("audio.pcm/1")},
        config_schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "pattern": "^tcp://"},
                "voice": {"type": "string"},
                "speaker": {"type": "string"},
                "streaming": {"enum": ["auto", "on", "off"]},
                "connect_timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["uri"],
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description=(
            "Any Wyoming TTS service; synthesize streaming when supported, "
            "otherwise sentence by sentence."
        ),
    )

    def __init__(self) -> None:
        self._address = Address("localhost", 10200)
        self._voice: SynthesizeVoice | None = None
        self._mode: Literal["auto", "on", "off"] = "auto"
        self._connect_timeout_s = DEFAULT_CONNECT_TIMEOUT_S
        self.service_streams: bool = False

    async def start(self, ctx: NodeContext) -> None:
        cfg = ctx.config
        self._address = Address.parse(str(cfg["uri"]))
        if cfg.get("voice") or cfg.get("speaker"):
            self._voice = SynthesizeVoice(name=cfg.get("voice"), speaker=cfg.get("speaker"))
        self._mode = cfg.get("streaming", "auto")
        self._connect_timeout_s = float(cfg.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S))
        info = await describe(self._address, self._connect_timeout_s)
        if info is not None and info.tts:
            self.service_streams = info.tts[0].supports_synthesize_streaming
            ctx.logger.info("Wyoming TTS %s: streaming=%s", info.tts[0].name, self.service_streams)

    async def stop(self) -> None:
        return None

    @property
    def streaming(self) -> bool:
        return self._mode == "on" or (self._mode == "auto" and self.service_streams)

    async def run_turn(self, turn: TurnSession) -> None:
        text: InputStream[Any] = turn.input("text")
        if await text.header() is None:
            return
        writer = _SpeechWriter(turn.output("speech"))
        if self.streaming:
            await self._run_streaming(text, writer)
        else:
            await self._run_sentences(text, writer, turn)

    # --- service supports synthesize streaming ---------------------------------------------------

    async def _run_streaming(self, text: InputStream[Any], writer: _SpeechWriter) -> None:
        async with connect(self._address, self._connect_timeout_s) as client:
            await client.write_event(SynthesizeStart(voice=self._voice).event())
            reader = asyncio.create_task(self._read_streamed(client, writer))
            try:
                full = ""
                try:
                    async for delta in text:
                        full += delta.text
                        await client.write_event(SynthesizeChunk(text=delta.text).event())
                    # Full text for services that only understand plain synthesize (HA does too).
                    await client.write_event(Synthesize(text=full, voice=self._voice).event())
                    await client.write_event(SynthesizeStop().event())
                except OSError as send_error:
                    # The service hung up; the reader has (or will get) its error event.
                    done, _ = await asyncio.wait({reader}, timeout=1.0)
                    reader_error = reader.exception() if reader in done else None
                    if isinstance(reader_error, WyomingError):
                        raise reader_error from send_error
                    raise WyomingError(f"service closed the connection: {send_error}") from None
                await reader
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader

    @staticmethod
    async def _read_streamed(client: AsyncTcpClient, writer: _SpeechWriter) -> None:
        while True:
            event = raise_on_error(await client.read_event())
            if AudioStart.is_type(event.type):
                await writer.start(AudioStart.from_event(event))
            elif WyAudioChunk.is_type(event.type):
                await writer.chunk(WyAudioChunk.from_event(event).audio)
            elif SynthesizeStopped.is_type(event.type):
                return

    # --- plain synthesize: one request per sentence, in order ------------------------------------

    async def _run_sentences(
        self, text: InputStream[Any], writer: _SpeechWriter, turn: TurnSession
    ) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        worker = asyncio.create_task(self._synthesize_queue(queue, writer, turn))
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
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _synthesize_queue(
        self, queue: asyncio.Queue[str | None], writer: _SpeechWriter, turn: TurnSession
    ) -> None:
        while (sentence := await queue.get()) is not None:
            turn.trace("wyoming.tts.sentence", chars=len(sentence))
            async with connect(self._address, self._connect_timeout_s) as client:
                await client.write_event(Synthesize(text=sentence, voice=self._voice).event())
                while True:
                    event = raise_on_error(await client.read_event())
                    if AudioStart.is_type(event.type):
                        await writer.start(AudioStart.from_event(event))
                    elif WyAudioChunk.is_type(event.type):
                        await writer.chunk(WyAudioChunk.from_event(event).audio)
                    elif AudioStop.is_type(event.type):
                        break
