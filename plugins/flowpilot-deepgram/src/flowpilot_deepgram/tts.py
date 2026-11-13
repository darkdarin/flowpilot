"""``deepgram.tts``: streaming text-to-speech with Deepgram Flux TTS (``/v2/speak``).

Text goes to Deepgram piece by piece as the agent writes it (``Speak``), and speech comes back
while the agent is still writing; ``Flush`` ends the answer and ``SpeechMetadata`` says all of its
audio has been sent. The voice is a model string, ``flux-<voice>-<language>``: it sets the language
too. Without a chosen voice the node takes the first voice of the turn's language.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    DYNAMIC_KEY,
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
from flowpilot_sdk.helpers import SpeechTiming

from .common import (
    API_KEY_SETTING,
    TTS_URL,
    DeepgramError,
    connect,
    error_of,
    http_session,
    language_base,
)
from .voices import VOICES

SAMPLE_RATES = [8000, 16000, 24000, 32000, 44100, 48000]
LANGUAGES = sorted({model.rsplit("-", 1)[1] for model, *_ in VOICES})


def voice_language(model: str) -> str:
    return model.rsplit("-", 1)[-1]


def default_voice(language: str | None) -> str | None:
    """The first (featured) voice of a language."""
    return next((m for m, *_ in VOICES if voice_language(m) == language), None)


class DeepgramTts:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="deepgram.tts",
        translations=node_translations(__package__, "deepgram.tts"),
        version="1.0.0",
        title="Deepgram text-to-speech",
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
                "language": {
                    "title": "Language",
                    "description": (
                        "Narrows the voices to one language; without a voice, the node speaks "
                        "the turn's language with its first voice."
                    ),
                    "type": "string",
                    "format": "language",
                    NODE_OPTIONS_KEY: True,
                },
                "voice": {
                    "title": "Voice",
                    "description": (
                        "A Flux voice, e.g. flux-olga-ru; it sets the language too. The first "
                        "voice of the language when not set."
                    ),
                    "type": "string",
                    NODE_OPTIONS_KEY: True,
                    DYNAMIC_KEY: True,  # e.g. a softer voice at night
                },
                "speed": {
                    "title": "Speed",
                    "description": "1 is normal, 0.5 half as fast, 1.5 the fastest; 0.05 steps.",
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 1.5,
                    "multipleOf": 0.05,
                    "default": 1.0,
                    DYNAMIC_KEY: True,
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
            "Speaks text with a Deepgram Flux voice, streaming: speech starts while the agent is "
            "still writing its answer."
        ),
    )

    def __init__(self) -> None:
        self._api_key = ""
        self._config: dict[str, Any] = {}

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        if field == "language":
            return [ConfigOption(code) for code in LANGUAGES]
        if field != "voice":
            return []
        language = language_base(config.get("language"))
        return [
            ConfigOption(
                model, name, " · ".join(x for x in (voice_language(model), gender, accent) if x)
            )
            for model, name, gender, accent in VOICES
            if language is None or voice_language(model) == language
        ]

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        self._api_key = ctx.secret(ctx.config["api_key"]["secret"])
        voice = self._config.get("voice")
        if voice and not str(voice).startswith("flux-"):
            raise DeepgramError(f"{voice!r} is not a Flux voice (flux-<voice>-<language>)")
        language = language_base(self._config.get("language"))
        if not voice and language and default_voice(language) is None:
            raise DeepgramError(f"Deepgram has no Flux voice for language {language!r}")

    async def stop(self) -> None:
        return None

    def _voice(self, turn_language: str | None, config: Mapping[str, Any] | None = None) -> str:
        config = self._config if config is None else config
        if voice := config.get("voice"):
            return str(voice)
        language = language_base(config.get("language")) or language_base(turn_language)
        voice = default_voice(language) or default_voice("en")
        assert voice is not None
        return voice

    async def run_turn(self, turn: TurnSession) -> None:
        text: InputStream[Any] = turn.input("text")
        if await text.header() is None:
            return
        cfg = await turn.settings(self._config)  # voice or speed for this turn
        rate = int(self._config.get("sample_rate", 24000))
        voice = self._voice(turn.context.language, cfg)
        speech = turn.output("speech")
        await speech.open(AudioFormat(sample_rate=rate).as_meta())
        offset = 0
        timing = SpeechTiming()

        async def deliver(pcm: bytes) -> None:
            nonlocal offset
            await speech.send(AudioChunk(pcm, offset))
            offset += len(pcm) // 2
            timing.audio(len(pcm) // 2, rate)

        params = [("model", voice), ("encoding", "linear16"), ("sample_rate", str(rate))]
        speed = float(cfg.get("speed") or 1.0)
        if abs(speed - 1.0) > 1e-9:
            params.append(("speed", f"{speed:g}"))
        synthesis = _Synthesis(self._api_key, params, deliver)
        try:
            await synthesis.connect()  # in parallel with the agent's first words
            async for piece in text:
                timing.text()
                await synthesis.speak(piece.text)
            await synthesis.finish()
        finally:
            await synthesis.close()
        turn.trace(
            "deepgram.tts",
            **timing.attrs(),
            voice=voice,
            warnings=synthesis.warnings,
        )


class _Synthesis:
    """One turn's synthesis on one connection: text in pieces, PCM s16le mono out."""

    def __init__(
        self,
        api_key: str,
        params: list[tuple[str, str]],
        on_audio: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._api_key = api_key
        self._params = params
        self._on_audio = on_audio
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._done = asyncio.Event()  # the answer's audio is all here, or the connection ended
        self._error: DeepgramError | None = None
        self._odd_byte = b""
        self._spoken = False
        self.warnings: list[str] = []

    async def connect(self) -> None:
        self._http = http_session()
        self._ws = await connect(self._http, TTS_URL, self._params, self._api_key)
        self._reader = asyncio.create_task(self._read(self._ws), name="deepgram tts reader")

    async def speak(self, text: str) -> None:
        self._raise_error()
        if not text:
            return
        await self._send({"type": "Speak", "text": text})
        self._spoken = True

    async def finish(self) -> None:
        """No more text: wait until all audio has been delivered."""
        self._raise_error()
        if self._spoken:  # a Flush without text gets a warning, not SpeechMetadata
            await self._send({"type": "Flush"})
            await self._done.wait()
        self._raise_error()

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(ConnectionError):
                await self._ws.send_str(json.dumps({"type": "Close"}))
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

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._ws is not None
        try:
            await self._ws.send_str(json.dumps(message, ensure_ascii=False))
        except ConnectionError as err:
            raise DeepgramError(f"connection closed: {err}") from err

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._deliver(msg.data)
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if (err := error_of(data)) is not None:
                    self._error = self._error or err
                    return
                if data.get("type") == "Warning":
                    self.warnings.append(str(data.get("code")))
                elif data.get("type") == "SpeechMetadata":
                    return
            if self._spoken and self._error is None:
                self._error = DeepgramError(
                    f"the connection closed before the audio ended ({ws.close_code})"
                )
        finally:
            self._done.set()  # waiting callers must not hang

    async def _deliver(self, audio: bytes) -> None:
        audio = self._odd_byte + audio  # whole 16-bit samples only
        audio, self._odd_byte = audio[: len(audio) // 2 * 2], audio[len(audio) // 2 * 2 :]
        if not audio:
            return
        await self._on_audio(audio)
