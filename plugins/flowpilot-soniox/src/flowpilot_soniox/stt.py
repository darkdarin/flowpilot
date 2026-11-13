"""``soniox.stt``: streaming speech-to-text over the Soniox real-time WebSocket API.

The turn's audio is sent as it comes; partial results go out on ``transcript`` while the user
speaks. Soniox detects the end of the command itself (``<end>``): the node then closes its audio
input, so the satellite stops streaming, and sends the final text. If the audio ends first, the
node asks Soniox to finalize what it heard (``<fin>``).

Some satellites send audio from before the command, wake word included; with ``wake_words`` set
the node cuts the wake word (and anything before it) from the text. An endpoint right after the
wake word alone does not end the command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    NODE_OPTIONS_KEY,
    AudioFormat,
    ConfigOption,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    StreamError,
    Transcript,
    TurnSession,
    Utterance,
    node_translations,
)
from flowpilot_sdk.wake_word import Token, strip_wake_word

from .common import (
    API_KEY_SETTING,
    LANGUAGES,
    STT_URL,
    SonioxError,
    api_key_of,
    error_of,
    get_json,
    language_base,
)

END_TOKEN = "<end>"
FIN_TOKEN = "<fin>"
CONNECT_TIMEOUT_S = 10.0
FINAL_TIMEOUT_S = 10.0  # after the audio ended, how long Soniox may take to finalize
WAKE_WORD_WINDOW_MS = 3000


class SonioxStt:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="soniox.stt",
        translations=node_translations(__package__, "soniox.stt"),
        version="1.0.0",
        title="Soniox speech-to-text",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1", description="Speech to recognize.")},
        outputs={
            "transcript": PortSpec(
                "text.transcript/1",
                description="Partial results while the user speaks, then the final text.",
            ),
            "utterance": PortSpec(
                "text.utterance/1",
                kind="value",
                description="The final text, when something was said.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "api_key": API_KEY_SETTING,
                "model": {
                    "title": "Model",
                    "description": "A real-time Soniox model; stt-rt-v5 when not set.",
                    "type": "string",
                    "default": "stt-rt-v5",
                    NODE_OPTIONS_KEY: True,
                },
                "language": {
                    "title": "Language",
                    "description": "What the user speaks; the language of the turn when not set.",
                    "type": "string",
                    "format": "language",
                    NODE_OPTIONS_KEY: True,
                },
                "detect_language": {
                    "title": "Detect the language",
                    "description": (
                        "Hear what language is spoken (the chosen one is a hint) and tell it on "
                        "with the text, so the answer and its voice can follow; on when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "language_hints_strict": {
                    "title": "Only this language",
                    "description": (
                        "Without language detection: recognize only the chosen language, not "
                        "words of others that sound alike; on when not set."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "context_terms": {
                    "title": "Terms",
                    "description": (
                        "Words Soniox should expect: names of rooms, devices, people; one per line."
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                },
                "context_text": {
                    "title": "Context",
                    "description": (
                        'Free text about the setting, e.g. "Voice commands in a smart home".'
                    ),
                    "type": "string",
                },
                "endpoint_sensitivity": {
                    "title": "End-of-speech sensitivity",
                    "description": (
                        "Higher ends the command sooner after the user stops, lower waits through "
                        "pauses; 0.3 when not set."
                    ),
                    "type": "number",
                    "minimum": -1,
                    "maximum": 1,
                    "default": 0.3,
                },
                "endpoint_latency_adjustment_level": {
                    "title": "End-of-speech speed",
                    "description": (
                        "0 waits for certainty, 3 answers fastest and may cut slow speakers; 2 "
                        "when not set."
                    ),
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                    "default": 2,
                },
                "max_endpoint_delay_ms": {
                    "title": "Longest end-of-speech wait, ms",
                    "description": "The command ends at the latest this long after speech; 1500.",
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 3000,
                    "default": 1500,
                },
                "max_utterance_s": {
                    "title": "Longest command, s",
                    "description": "Finalize after this much audio even if the user keeps talking.",
                    "type": "number",
                    "minimum": 3,
                    "maximum": 60,
                    "default": 15,
                },
                "wake_words": {
                    "title": "Wake words to cut",
                    "description": (
                        "For satellites that send the wake word with the command: cut it (and "
                        "anything before it) from the text; one per line, e.g. Alexa."
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["api_key"],
            "additionalProperties": False,
        },
        turn_timeout_s=90.0,
        description=(
            "Recognizes speech with Soniox in real time: partial results while the user speaks, "
            "and its own end-of-speech detection, so no voice command segmenter is needed."
        ),
    )

    def __init__(self) -> None:
        self._api_key = ""
        self._config: dict[str, Any] = {}

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        if field == "language":
            return [ConfigOption(code) for code in LANGUAGES]
        if field == "model" and (key := api_key_of(config)):
            data = await get_json("models", key)
            return [
                ConfigOption(str(m["id"]), str(m.get("name") or m["id"]))
                for m in data.get("models", [])
                # real-time models only (stt-rt-…); async ones cannot stream
                if m.get("transcription_mode") in (None, "real_time") or "-rt-" in str(m["id"])
            ]
        return []

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        self._api_key = ctx.secret(ctx.config["api_key"]["secret"])

    async def stop(self) -> None:
        return None

    @property
    def _detects(self) -> bool:
        return bool(self._config.get("detect_language", True))

    def _session_config(self, fmt: AudioFormat, language: str | None) -> dict[str, Any]:
        cfg = self._config
        config: dict[str, Any] = {
            "model": cfg.get("model", "stt-rt-v5"),
            "audio_format": "pcm_s16le",
            "sample_rate": fmt.sample_rate,
            "num_channels": fmt.channels,
            "enable_endpoint_detection": True,
            "endpoint_latency_adjustment_level": int(
                cfg.get("endpoint_latency_adjustment_level", 2)
            ),
            "endpoint_sensitivity": float(cfg.get("endpoint_sensitivity", 0.3)),
            "max_endpoint_delay_ms": int(cfg.get("max_endpoint_delay_ms", 1500)),
        }
        detect = self._detects
        if detect:
            config["enable_language_identification"] = True
        if language:
            config["language_hints"] = [language]
            config["language_hints_strict"] = not detect and bool(
                cfg.get("language_hints_strict", True)
            )
        terms = list(cfg.get("context_terms") or [])
        terms += [w for w in cfg.get("wake_words") or [] if w not in terms]
        context: dict[str, Any] = {"terms": terms} if terms else {}
        if cfg.get("context_text"):
            context["text"] = str(cfg["context_text"])
        if context:
            config["context"] = context
        return config

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return  # a text turn
        fmt = AudioFormat.from_meta(header.meta)
        language = language_base(self._config.get("language")) or language_base(
            turn.context.language
        )
        recognition = _Recognition(
            turn, list(self._config.get("wake_words") or []), language or turn.context.language
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.ws_connect(
                STT_URL, headers={"Authorization": f"Bearer {self._api_key}"}, compress=0
            ) as ws,
        ):
            await ws.send_str(json.dumps(self._session_config(fmt, language)))
            limit = int(float(self._config.get("max_utterance_s", 15)) * fmt.sample_rate)
            sender = asyncio.create_task(recognition.send_audio(ws, audio, fmt, limit))
            try:
                ended_by = await recognition.receive(ws, sender)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError, StreamError):
                    await sender
        audio.close()  # Soniox decided the command is over: the satellite can stop streaming
        text = recognition.text
        heard = (recognition.language if self._detects else None) or language
        turn.trace("soniox.stt", **recognition.stats(ended_by, fmt), language=heard)
        await turn.output("transcript").send(Transcript(text, final=True, language=heard))
        if text:
            await turn.output("utterance").send(Utterance(text, heard))


class _Recognition:
    """The tokens of one turn and the WebSocket exchange that produces them."""

    def __init__(self, turn: TurnSession, wake_words: list[str], language: str | None) -> None:
        self._turn = turn
        self._wake_words = wake_words
        self._language = language
        self._final: list[Token] = []
        # letters of the final text by the language Soniox heard them in (language detection)
        self._letters: Counter[str] = Counter()
        self._partial_shown = ""
        self._samples = 0
        self._speech_end_ms = 0

    @property
    def text(self) -> str:
        return self._strip(self._final)[0]

    @property
    def language(self) -> str | None:
        """The language most of the text was heard in, with language detection on."""
        return self._letters.most_common(1)[0][0] if self._letters else None

    def _strip(self, tokens: list[Token]) -> tuple[str, bool]:
        if not self._wake_words:
            return "".join(t.text for t in tokens).strip(), False
        return strip_wake_word(tokens, self._wake_words, WAKE_WORD_WINDOW_MS)

    async def send_audio(
        self, ws: aiohttp.ClientWebSocketResponse, audio: InputStream[Any], fmt: AudioFormat,
        limit: int,
    ) -> None:  # fmt: skip
        frame = fmt.bytes_per_sample
        rest = b""
        async for chunk in audio:
            data = rest + chunk.pcm
            whole = len(data) - len(data) % frame  # Soniox rejects partial samples
            data, rest = data[:whole], data[whole:]
            if data:
                await ws.send_bytes(data)
                self._samples += len(data) // frame
            if self._samples >= limit:
                self._turn.trace("soniox.stt.limit", seconds=self._samples / fmt.sample_rate)
                break
        await ws.send_str(json.dumps({"type": "finalize"}))

    async def receive(self, ws: aiohttp.ClientWebSocketResponse, sender: asyncio.Task[None]) -> str:
        """Read results until the utterance is over; return what ended it.

        One read stays pending across iterations; a failed audio input (or send) ends the turn
        with its error, and once the audio is over Soniox must finalize in time.
        """
        read: asyncio.Future[aiohttp.WSMessage] | None = None
        try:
            while True:
                if read is None:
                    read = asyncio.ensure_future(ws.receive())
                waiting: set[asyncio.Future[Any]] = {read}
                if not sender.done():
                    waiting.add(sender)
                done, _ = await asyncio.wait(
                    waiting,
                    timeout=FINAL_TIMEOUT_S if sender.done() else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if sender in done and not sender.cancelled() and sender.exception():
                    raise sender.exception()  # type: ignore[misc]
                if read not in done:
                    if done:
                        continue  # the audio ended; wait for the final result
                    raise SonioxError(f"no final result within {FINAL_TIMEOUT_S:.0f}s")
                msg, read = read.result(), None
                if (ended := await self._handle(ws, msg)) is not None:
                    return ended
        finally:
            if read is not None:
                read.cancel()

    async def _handle(
        self, ws: aiohttp.ClientWebSocketResponse, msg: aiohttp.WSMessage
    ) -> str | None:
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            raise SonioxError("Soniox closed the connection before the result")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise SonioxError(f"connection failed: {ws.exception()}")
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        data = json.loads(msg.data)
        if (err := error_of(data)) is not None:
            raise err
        partial: list[Token] = []
        for token in data.get("tokens", []):
            text = str(token.get("text", ""))
            if not token.get("is_final"):
                partial.append(Token(text, int(token.get("start_ms", 0))))
                continue
            if text == END_TOKEN:
                if self._only_wake_word():
                    continue  # the wake word alone: keep listening for the command
                return "end"
            if text == FIN_TOKEN:
                return "finalize"
            self._final.append(Token(text, int(token.get("start_ms", 0))))
            if heard := token.get("language"):
                self._letters[str(heard)] += sum(c.isalpha() for c in text)
            if text.strip() and "end_ms" in token:
                self._speech_end_ms = int(token["end_ms"])
        if data.get("finished"):
            return "finished"
        await self._show_partial(partial)
        return None

    def _only_wake_word(self) -> bool:
        text, found = self._strip(self._final)
        return found and not text

    async def _show_partial(self, partial: list[Token]) -> None:
        text = self._strip(self._final + partial)[0]
        if text and text != self._partial_shown:
            self._partial_shown = text
            await self._turn.output("transcript").send(
                Transcript(text, final=False, language=self.language or self._language)
            )

    def stats(self, ended_by: str, fmt: AudioFormat) -> dict[str, Any]:
        _, found = self._strip(self._final)
        return {
            "ended_by": ended_by,
            "chars": len(self.text),
            "audio_s": round(self._samples / fmt.sample_rate, 2),
            "speech_end_s": round(self._speech_end_ms / 1000, 2),
            "wake_word_cut": found,
        }
