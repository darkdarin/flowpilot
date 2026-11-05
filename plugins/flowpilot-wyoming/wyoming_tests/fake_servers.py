"""Fake Wyoming services modelled on real ones.

Soniox-like ASR (own end-of-speech detection, partials), Whisper-like ASR, Piper-like TTS.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from wyoming.asr import Transcribe, Transcript, TranscriptChunk, TranscriptStart, TranscriptStop
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import (
    AsrModel,
    AsrProgram,
    Attribution,
    Describe,
    Info,
    TtsProgram,
    TtsVoice,
    TtsVoiceSpeaker,
)
from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

ATTR = Attribution(name="test", url="https://example.invalid")


@dataclass
class Record:
    """What the fake service saw, for assertions."""

    transcribe: list[Transcribe] = field(default_factory=list)
    audio_chunks: int = 0
    audio_stopped: bool = False
    synthesize: list[str] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    connections: int = 0


class AsrHandler(AsyncEventHandler):
    """``own_vad``: finalize after ``end_after`` chunks (like Soniox); else wait for audio-stop."""

    def __init__(self, record: Record, opts: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        own_vad, streaming = opts["own_vad"], opts["streaming"]
        end_after, fail, words = opts["end_after"], opts["fail"], opts["words"]
        self.r, self.own_vad, self.streaming = record, own_vad, streaming
        self.end_after, self.fail, self.words = end_after, fail, words
        self.n = 0
        self.done = False
        record.connections += 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            program = AsrProgram(
                name="fake-asr", description="", attribution=ATTR, installed=True, version="1",
                models=[AsrModel(name="m", description="", attribution=ATTR, installed=True,
                                 languages=["en"], version="1"),
                        AsrModel(name="large", description="Large model", attribution=ATTR,
                                 installed=True, languages=["ru", "en"], version="3"),
                        AsrModel(name="missing", description="", attribution=ATTR,
                                 installed=False, languages=["de"], version="1")],
                supports_transcript_streaming=self.streaming,
                requires_external_vad=not self.own_vad,
            )  # fmt: skip
            await self.write_event(Info(asr=[program]).event())
            return True
        if Transcribe.is_type(event.type):
            self.r.transcribe.append(Transcribe.from_event(event))
            if self.fail:
                await self.write_event(Error(text="model exploded", code="boom").event())
                return bool(self.fail != "abrupt")  # polite services keep reading
            if self.streaming:
                await self.write_event(TranscriptStart(language="en").event())
            return True
        if AudioChunk.is_type(event.type) and not self.done:
            self.n += 1
            self.r.audio_chunks += 1
            if self.streaming and self.n % 5 == 0 and self.n // 5 <= len(self.words):
                await self.write_event(
                    TranscriptChunk(text=self.words[self.n // 5 - 1] + " ").event()
                )
            if self.own_vad and self.n >= self.end_after:
                await self._final()
            return True
        if AudioStop.is_type(event.type):
            self.r.audio_stopped = True
            if not self.done:
                await self._final()
            return True
        return True

    async def _final(self) -> None:
        self.done = True
        if self.streaming:
            await self.write_event(TranscriptStop().event())
        await self.write_event(Transcript(text=" ".join(self.words), language="en").event())


class TtsHandler(AsyncEventHandler):
    def __init__(self, record: Record, opts: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        streaming, fail = opts["streaming"], opts["fail"]
        self.r, self.streaming, self.fail = record, streaming, fail
        self.in_stream = False
        self.started = False
        record.connections += 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            program = TtsProgram(
                name="fake-tts", description="", attribution=ATTR, installed=True, version="1",
                voices=[TtsVoice(name="v", description="", attribution=ATTR, installed=True,
                                 languages=["en"], version="1"),
                        TtsVoice(name="anna", description="Anna", attribution=ATTR, installed=True,
                                 languages=["ru"], version="1",
                                 speakers=[TtsVoiceSpeaker(name="calm"),
                                           TtsVoiceSpeaker(name="lively")])],
                supports_synthesize_streaming=self.streaming,
            )  # fmt: skip
            await self.write_event(Info(tts=[program]).event())
            return True
        if SynthesizeStart.is_type(event.type):
            self.in_stream = True
            return True
        if SynthesizeChunk.is_type(event.type):
            text = SynthesizeChunk.from_event(event).text
            self.r.chunks.append(text)
            await self._audio(text, stop=False)
            return True
        if Synthesize.is_type(event.type):
            text = Synthesize.from_event(event).text
            self.r.synthesize.append(text)
            if self.fail:
                await self.write_event(Error(text="no voice").event())
                return False
            if not self.in_stream:
                await self._audio(text, stop=True)
                return False  # one request per connection, like Piper
            return True
        if SynthesizeStop.is_type(event.type):
            await self.write_event(AudioStop().event())
            await self.write_event(SynthesizeStopped().event())
            return False
        return True

    async def _audio(self, text: str, *, stop: bool) -> None:
        if not self.started:
            await self.write_event(AudioStart(rate=22050, width=2, channels=1).event())
            self.started = True
        await self.write_event(
            AudioChunk(rate=22050, width=2, channels=1, audio=text.encode()).event()
        )
        if stop:
            await self.write_event(AudioStop().event())


@asynccontextmanager
async def serve(factory: Callable[..., AsyncEventHandler]) -> AsyncIterator[str]:
    server = AsyncTcpServer("127.0.0.1", 0)
    await server.start(factory)
    assert isinstance(server._server, asyncio.Server)
    port = server._server.sockets[0].getsockname()[1]
    try:
        yield f"tcp://127.0.0.1:{port}"
    finally:
        await server.stop()
        await asyncio.sleep(0)


Factory = Callable[..., AsyncEventHandler]


def asr(
    record: Record,
    *,
    own_vad: bool = True,
    streaming: bool = True,
    end_after: int = 15,
    fail: bool | str = False,
    words: list[str] | None = None,
) -> Factory:
    w = words or ["turn", "on", "the", "light"]
    opts = {"own_vad": own_vad, "streaming": streaming, "end_after": end_after, "fail": fail,
            "words": w}  # fmt: skip
    return lambda *a, **k: AsrHandler(record, opts, *a, **k)


def tts(record: Record, *, streaming: bool = False, fail: bool = False) -> Factory:
    opts = {"streaming": streaming, "fail": fail}
    return lambda *a, **k: TtsHandler(record, opts, *a, **k)
