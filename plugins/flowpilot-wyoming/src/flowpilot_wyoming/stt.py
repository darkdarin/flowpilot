"""``wyoming.stt``: speech-to-text through any Wyoming ASR service."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

from wyoming.asr import Transcribe, TranscriptChunk, TranscriptStart, TranscriptStop
from wyoming.asr import Transcript as WyTranscript
from wyoming.audio import AudioChunk as WyAudioChunk
from wyoming.audio import AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import Event

from flowpilot_sdk import (
    OPTIONS_KEY,
    AudioFormat,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    StreamError,
    Transcript,
    TurnSession,
    Utterance,
)

from .common import (
    DEFAULT_CONNECT_TIMEOUT_S,
    Address,
    WyomingError,
    connect,
    describe,
    explain_send_failure,
    raise_on_error,
)


class WyomingStt:
    """Streams turn audio to a Wyoming ASR service and forwards its transcripts."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="wyoming.stt",
        version="1.0.0",
        title="Wyoming speech-to-text",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1")},
        outputs={
            "transcript": PortSpec("text.transcript/1"),
            "utterance": PortSpec("text.utterance/1", kind="value"),
        },
        config_schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string", "pattern": "^tcp://", OPTIONS_KEY: "wyoming.asr"},
                "language": {"type": "string"},
                "model": {"type": "string"},
                "connect_timeout_s": {"type": "number", "exclusiveMinimum": 0},
                "final_timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
            "required": ["uri"],
            "additionalProperties": False,
        },
        turn_timeout_s=60.0,
        description="Any Wyoming ASR service; partial transcripts when the service streams them.",
    )

    def __init__(self) -> None:
        self._address = Address("localhost", 10300)
        self._language: str | None = None
        self._model: str | None = None
        self._connect_timeout_s = DEFAULT_CONNECT_TIMEOUT_S
        self._final_timeout_s = 15.0
        self.streams_partials: bool | None = None  # from describe; None = unknown

    async def start(self, ctx: NodeContext) -> None:
        cfg = ctx.config
        self._address = Address.parse(str(cfg["uri"]))
        self._language = cfg.get("language")
        self._model = cfg.get("model")
        self._connect_timeout_s = float(cfg.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S))
        self._final_timeout_s = float(cfg.get("final_timeout_s", 15.0))
        info = await describe(self._address, self._connect_timeout_s)
        if info is not None and info.asr:
            program = info.asr[0]
            self.streams_partials = program.supports_transcript_streaming
            ctx.logger.info(
                "Wyoming ASR %s: streaming=%s, own VAD=%s",
                program.name,
                program.supports_transcript_streaming,
                not program.requires_external_vad,
            )

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return  # text turn
        fmt = AudioFormat.from_meta(header.meta)
        language = self._language or turn.context.language
        async with connect(self._address, self._connect_timeout_s) as client:
            try:
                await client.write_event(Transcribe(name=self._model, language=language).event())
                await client.write_event(
                    AudioStart(rate=fmt.sample_rate, width=2, channels=fmt.channels).event()
                )
            except OSError as err:
                raise await explain_send_failure(client, err) from err
            sender = asyncio.create_task(self._send_audio(client, audio, fmt))
            try:
                final = await self._receive(client, turn, sender)
            finally:
                sender.cancel()
                # Send failures were already handled in _receive.
                with contextlib.suppress(asyncio.CancelledError, OSError, StreamError):
                    await sender
        audio.close()  # the service decided the utterance is over
        text = final.text.strip()  # e.g. Whisper prefixes a space
        turn.trace("wyoming.stt.final", chars=len(text))
        await turn.output("transcript").send(
            Transcript(text, final=True, language=final.language or language)
        )
        if text:
            await turn.output("utterance").send(Utterance(text, final.language or language))

    @staticmethod
    async def _send_audio(
        client: AsyncTcpClient, audio: InputStream[Any], fmt: AudioFormat
    ) -> None:
        async for chunk in audio:
            await client.write_event(
                WyAudioChunk(
                    rate=fmt.sample_rate, width=2, channels=fmt.channels, audio=chunk.pcm
                ).event()
            )
        await client.write_event(AudioStop().event())

    async def _receive(
        self, client: AsyncTcpClient, turn: TurnSession, sender: asyncio.Task[None]
    ) -> WyTranscript:
        partial = ""
        # One read stays pending across iterations: cancelling a read mid-message would corrupt
        # the stream. A failed send is remembered; the service's own error event explains it.
        read: asyncio.Future[Event | None] | None = None
        send_error: BaseException | None = None
        while True:
            if read is None:
                read = asyncio.ensure_future(client.read_event())
            waiting: set[asyncio.Future[Any]] = {read}
            if not sender.done():
                waiting.add(sender)
            # No limit while audio still flows; after it ended the service must answer in time.
            done, _ = await asyncio.wait(
                waiting,
                timeout=self._final_timeout_s if sender.done() else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sender in done and not sender.cancelled() and send_error is None:
                send_error = sender.exception()
                if isinstance(send_error, StreamError):
                    raise send_error  # the audio input itself failed upstream
            if read not in done:
                if done:
                    continue  # only the sender finished; keep waiting for the service
                read.cancel()
                raise WyomingError(f"no final transcript within {self._final_timeout_s}s")
            try:
                result, read = read.result(), None
            except OSError as err:
                # A reset can discard an error event the service sent just before hanging up.
                raise WyomingError(f"connection to the service broke: {err}") from err
            if result is None and isinstance(send_error, OSError):
                raise await explain_send_failure(client, send_error) from send_error
            event = raise_on_error(result)
            if WyTranscript.is_type(event.type):
                return WyTranscript.from_event(event)
            if TranscriptStart.is_type(event.type):
                partial = ""
            elif TranscriptChunk.is_type(event.type):
                partial += TranscriptChunk.from_event(event).text
                await turn.output("transcript").send(Transcript(partial, final=False))
            elif TranscriptStop.is_type(event.type):
                continue
