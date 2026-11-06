"""``soniox.tts``: streaming text-to-speech over the Soniox real-time WebSocket API.

Text is sent piece by piece as the agent writes it, and speech comes back while the agent is still
writing, so the answer starts sounding early. Soniox ends a stream with ``request_timeout`` when
text stops coming for a few seconds (an agent calling a tool); the next piece starts a new stream,
on a new connection if Soniox closed the idle one.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    NODE_OPTIONS_KEY,
    AudioChunk,
    AudioFormat,
    ConfigOption,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    TurnSession,
    node_translations,
)

from .common import (
    API_KEY_SETTING,
    TTS_URL,
    SonioxError,
    api_key_of,
    error_of,
    get_json,
    language_base,
)

CONNECT_TIMEOUT_S = 10.0
MAX_TEXT_CHARS = 5000
SAMPLE_RATES = [8000, 16000, 22050, 24000, 44100, 48000]
_stream_ids = itertools.count(1)


async def tts_models(api_key: str) -> list[dict[str, Any]]:
    data = await get_json("tts-models", api_key)
    return list(data.get("models", []))


class SonioxTts:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="soniox.tts",
        translations=node_translations(__package__, "soniox.tts"),
        version="1.0.0",
        title="Soniox text-to-speech",
        category="speech",
        inputs={
            "text": PortSpec(
                "text.delta/1",
                description="Text to speak, as the agent writes it: speech starts right away.",
            )
        },
        outputs={"speech": PortSpec("audio.pcm/1", description="Synthesized speech.")},
        config_schema={
            "type": "object",
            "properties": {
                "api_key": API_KEY_SETTING,
                "voice": {
                    "title": "Voice",
                    "description": "The model's first voice when not set.",
                    "type": "string",
                    NODE_OPTIONS_KEY: True,
                },
                "model": {
                    "title": "Model",
                    "description": "The first text-to-speech model Soniox offers when not set.",
                    "type": "string",
                    NODE_OPTIONS_KEY: True,
                },
                "language": {
                    "title": "Language",
                    "description": "Language of the speech; the language of the turn when not set.",
                    "type": "string",
                    "format": "language",
                    NODE_OPTIONS_KEY: True,
                },
                "speed": {
                    "title": "Speed",
                    "description": "1 is normal, 0.5 half as fast, 2 twice as fast.",
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 2,
                    "default": 1.0,
                },
                "sample_rate": {
                    "title": "Sample rate, Hz",
                    "description": "Of the synthesized speech; 24000 when not set.",
                    "enum": SAMPLE_RATES,
                    ENUM_TITLES_KEY: {str(r): f"{r / 1000:g} kHz" for r in SAMPLE_RATES},
                    "default": 24000,
                },
            },
            "required": ["api_key"],
            "additionalProperties": False,
        },
        turn_timeout_s=120.0,
        description=(
            "Speaks text with a Soniox voice, streaming: speech starts while the agent is still "
            "writing its answer."
        ),
    )

    def __init__(self) -> None:
        self._api_key = ""
        self._config: dict[str, Any] = {}
        self._model = ""
        self._voice = ""
        self._languages: list[str] = []

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        key = api_key_of(config)
        if key is None or field not in ("model", "voice", "language"):
            return []
        models = await tts_models(key)
        if field == "model":
            return [ConfigOption(str(m["id"]), str(m.get("name") or m["id"])) for m in models]
        chosen = [m for m in models if m["id"] == config.get("model")] or models[:1]
        if field == "voice":
            return [
                ConfigOption(
                    str(v["id"]),
                    str(v["id"]),
                    " · ".join(str(x) for x in (v.get("gender"), v.get("description")) if x),
                )
                for m in chosen
                for v in m.get("voices", [])
            ]
        return [ConfigOption(str(lang["code"])) for m in chosen for lang in m.get("languages", [])]

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        self._api_key = ctx.secret(ctx.config["api_key"]["secret"])
        models = await tts_models(self._api_key)
        if not models:
            raise SonioxError("Soniox offers no text-to-speech models")
        wanted = self._config.get("model")
        model = next((m for m in models if m["id"] == wanted), None)
        if model is None:
            if wanted:
                raise SonioxError(f"Soniox has no text-to-speech model {wanted!r}")
            model = models[0]
        voices = [str(v["id"]) for v in model.get("voices", [])]
        voice = self._config.get("voice") or (voices[0] if voices else "")
        if voices and voice not in voices:
            raise SonioxError(f"model {model['id']} has no voice {voice!r}")
        self._model, self._voice = str(model["id"]), str(voice)
        self._languages = [str(lang["code"]) for lang in model.get("languages", [])]

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        text: InputStream[Any] = turn.input("text")
        if await text.header() is None:
            return
        rate = int(self._config.get("sample_rate", 24000))
        language = language_base(self._config.get("language")) or language_base(
            turn.context.language
        )
        if self._languages and language not in self._languages:
            language = self._languages[0]
        speech = turn.output("speech")
        await speech.open(AudioFormat(sample_rate=rate).as_meta())
        offset = 0

        async def deliver(pcm: bytes) -> None:
            nonlocal offset
            await speech.send(AudioChunk(pcm, offset))
            offset += len(pcm) // 2

        session = _Synthesis(
            self._api_key,
            {
                "model": self._model,
                "voice": self._voice,
                "language": language,
                "audio_format": "pcm_s16le",
                "sample_rate": rate,
            }
            | ({"speed": float(self._config["speed"])} if self._config.get("speed") else {}),
            deliver,
        )
        try:
            await session.connect()  # in parallel with the agent's first words
            async for piece in text:
                await session.add_text(piece.text)
            await session.finish()
        finally:
            await session.close()
        turn.trace(
            "soniox.tts",
            seconds=round(offset / rate, 2),
            first_audio_ms=session.first_audio_ms,
            streams=session.streams,
            voice=self._voice,
        )


class _Synthesis:
    """One turn's synthesis: text in (whole or in pieces), PCM s16le mono out."""

    def __init__(
        self,
        api_key: str,
        stream_config: dict[str, Any],
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._api_key = api_key
        self._stream_config = stream_config
        self._on_audio = on_audio
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stream_id: str | None = None
        self._terminated: dict[str, asyncio.Event] = {}
        self._error: SonioxError | None = None
        self._odd_byte = b""
        self._started = time.monotonic()
        self.first_audio_ms: int | None = None
        self.streams = 0

    async def connect(self) -> None:
        if self._ws is not None and self._reader is not None and not self._reader.done():
            return
        if self._http is None:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
            )
        try:
            self._ws = await self._http.ws_connect(
                TTS_URL, headers={"Authorization": f"Bearer {self._api_key}"}, compress=0
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SonioxError(f"cannot connect to Soniox: {type(err).__name__}: {err}") from err
        self._reader = asyncio.create_task(self._read(self._ws), name="soniox tts reader")

    async def add_text(self, text: str) -> None:
        self._raise_error()
        for start in range(0, len(text), MAX_TEXT_CHARS):
            if self._stream_id is None or self._terminated[self._stream_id].is_set():
                await self._start_stream()
            assert self._stream_id is not None
            chunk = text[start : start + MAX_TEXT_CHARS]
            await self._send({"stream_id": self._stream_id, "text": chunk, "text_end": False})

    async def finish(self) -> None:
        """No more text: wait until all audio has been delivered."""
        self._raise_error()
        if self._stream_id is not None:
            stream_id = self._stream_id
            if not self._terminated[stream_id].is_set():
                await self._send({"stream_id": stream_id, "text": "", "text_end": True})
                await self._terminated[stream_id].wait()
            self._stream_id = None
        self._raise_error()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error

    async def _start_stream(self) -> None:
        await self.connect()
        stream_id = f"s{next(_stream_ids)}"
        self._stream_id = stream_id
        self._terminated[stream_id] = asyncio.Event()
        self.streams += 1
        await self._send({"stream_id": stream_id, **self._stream_config})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._ws is not None
        try:
            await self._ws.send_str(json.dumps(message, ensure_ascii=False))
        except ConnectionError as err:
            raise SonioxError(f"connection closed: {err}") from err

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                stream_id = data.get("stream_id")
                if (err := error_of(data)) is not None:
                    if err.error_type == "request_timeout" and stream_id == self._stream_id:
                        pass  # text paused too long; the next piece starts a new stream
                    elif self._error is None:
                        self._error = err
                if data.get("audio"):
                    await self._deliver(base64.b64decode(data["audio"]))
                if data.get("terminated") and stream_id in self._terminated:
                    self._terminated[stream_id].set()
        finally:
            # Streams of a closed connection are over; waiting callers must not hang.
            current = self._stream_id
            if current is not None and not self._terminated[current].is_set():
                if self._error is None:
                    self._error = SonioxError("the connection closed before the audio ended")
            for event in self._terminated.values():
                event.set()

    async def _deliver(self, audio: bytes) -> None:
        audio = self._odd_byte + audio  # whole 16-bit samples only
        audio, self._odd_byte = audio[: len(audio) // 2 * 2], audio[len(audio) // 2 * 2 :]
        if not audio:
            return
        if self.first_audio_ms is None:
            self.first_audio_ms = round(1000 * (time.monotonic() - self._started))
        await self._on_audio(audio)
