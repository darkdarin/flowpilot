"""``deepgram.stt``: streaming speech-to-text with Deepgram Flux (``/v2/listen``).

Flux is built for conversations: it reports the turn as it goes (``TurnInfo``: ``Update``, then
``EndOfTurn``) and decides itself when the user is done. The node sends the turn's audio in 80 ms
frames, as Deepgram recommends; partial results go out on ``transcript``. On ``EndOfTurn`` it
closes its audio input, so the satellite stops streaming, and sends the final text. If the audio
ends first, the node asks Flux to end the turn now (``ForceEndTurn``).

With ``wake_words`` set the node cuts the wake word (and anything before it) from the text; a turn
that is the wake word alone does not end the command.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar

import aiohttp

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
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
    STT_LANGUAGES,
    STT_MODELS,
    STT_URL,
    DeepgramError,
    connect,
    error_of,
    http_session,
    language_base,
)

FRAME_MS = 80
FINAL_TIMEOUT_S = 10.0  # after the audio ended, how long Flux may take to end the turn
WAKE_WORD_WINDOW_MS = 3000
NO_ACTIVE_TURN = "FORCE_END_TURN_NO_ACTIVE_TURN"


class DeepgramStt:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="deepgram.stt",
        translations=node_translations(__package__, "deepgram.stt"),
        version="1.0.0",
        title="Deepgram speech-to-text",
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
                    "description": (
                        "flux-general-multi speaks ten languages, Russian among them; "
                        "flux-general-en is English only. flux-general-multi when not set."
                    ),
                    "type": "string",
                    "enum": STT_MODELS,
                    ENUM_TITLES_KEY: {
                        "flux-general-multi": "Multilingual (flux-general-multi)",
                        "flux-general-en": "English (flux-general-en)",
                    },
                    "default": "flux-general-multi",
                },
                "language": {
                    "title": "Language",
                    "description": (
                        "What the user speaks (a hint for flux-general-multi); the language of "
                        "the turn when not set."
                    ),
                    "type": "string",
                    "format": "language",
                    NODE_OPTIONS_KEY: True,
                },
                "keyterms": {
                    "title": "Terms",
                    "description": (
                        "Words Deepgram should expect: names of rooms, devices, people. A JSON "
                        'list of strings, e.g. ["garland", "kitchen"].'
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                },
                "eot_threshold": {
                    "title": "End-of-speech confidence",
                    "description": (
                        "How sure Flux must be that the user is done: lower ends the command "
                        "sooner and may cut pauses, higher waits; 0.7 when not set."
                    ),
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 1,
                    "default": 0.7,
                },
                "eot_timeout_ms": {
                    "title": "Longest end-of-speech wait, ms",
                    "description": (
                        "The command ends at the latest this long after speech, however unsure "
                        "Flux is; 3000."
                    ),
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 10000,
                    "default": 3000,
                },
                "max_utterance_s": {
                    "title": "Longest command, s",
                    "description": "End the command after this much audio, even mid-sentence.",
                    "type": "number",
                    "minimum": 3,
                    "maximum": 60,
                    "default": 15,
                },
                "wake_words": {
                    "title": "Wake words to cut",
                    "description": (
                        "For satellites that send the wake word with the command: cut it (and "
                        'anything before it) from the text. A JSON list, e.g. ["Alexa"].'
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
            "Recognizes speech with Deepgram Flux in real time: partial results while the user "
            "speaks, and its own end-of-speech detection, so no voice command segmenter is needed."
        ),
    )

    def __init__(self) -> None:
        self._api_key = ""
        self._config: dict[str, Any] = {}

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        if field == "language":
            return [ConfigOption(code) for code in STT_LANGUAGES]
        return []

    async def start(self, ctx: NodeContext) -> None:
        self._config = dict(ctx.config)
        self._api_key = ctx.secret(ctx.config["api_key"]["secret"])

    async def stop(self) -> None:
        return None

    def _params(self, fmt: AudioFormat, language: str | None) -> list[tuple[str, str]]:
        cfg = self._config
        model = str(cfg.get("model") or "flux-general-multi")
        params = [
            ("model", model),
            ("encoding", "linear16"),
            ("sample_rate", str(fmt.sample_rate)),
            ("eot_threshold", str(float(cfg.get("eot_threshold", 0.7)))),
            ("eot_timeout_ms", str(int(cfg.get("eot_timeout_ms", 3000)))),
        ]
        if language and model != "flux-general-en":
            params.append(("language_hint", language))
        terms = [str(t) for t in cfg.get("keyterms") or [] if str(t).strip()]
        terms += [w for w in cfg.get("wake_words") or [] if w not in terms]
        params += [("keyterm", term) for term in terms]
        return params

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return  # a text turn
        fmt = AudioFormat.from_meta(header.meta)
        if fmt.channels != 1:
            raise DeepgramError(f"Flux takes mono audio, got {fmt.channels} channels")
        language = language_base(self._config.get("language")) or language_base(
            turn.context.language
        )
        if language not in STT_LANGUAGES:
            language = None  # let Flux detect it
        recognition = _Recognition(
            turn, list(self._config.get("wake_words") or []), language or turn.context.language
        )
        async with http_session() as session:
            ws = await connect(session, STT_URL, self._params(fmt, language), self._api_key)
            async with ws:
                limit = int(float(self._config.get("max_utterance_s", 15)) * fmt.sample_rate)
                sender = asyncio.create_task(recognition.send_audio(ws, audio, fmt, limit))
                try:
                    ended_by = await recognition.receive(ws, sender)
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ConnectionError, StreamError):
                        await sender
                with contextlib.suppress(ConnectionError):
                    await ws.send_str(json.dumps({"type": "CloseStream"}))
        audio.close()  # the command is over: the satellite can stop streaming
        text = recognition.text
        turn.trace("deepgram.stt", **recognition.stats(ended_by, fmt))
        out_language = recognition.language
        await turn.output("transcript").send(Transcript(text, final=True, language=out_language))
        if text:
            await turn.output("utterance").send(Utterance(text, out_language))


class _Recognition:
    """The words of one turn and the WebSocket exchange that produces them."""

    def __init__(self, turn: TurnSession, wake_words: list[str], language: str | None) -> None:
        self._turn = turn
        self._wake_words = wake_words
        self.language = language
        self._words: list[Token] = []
        self._partial_shown = ""
        self._samples = 0
        self._speech_end_s = 0.0
        self._turns = 0

    @property
    def text(self) -> str:
        return self._strip(self._words)[0]

    def _strip(self, words: list[Token]) -> tuple[str, bool]:
        if not self._wake_words:
            return "".join(w.text for w in words).strip(), False
        return strip_wake_word(words, self._wake_words, WAKE_WORD_WINDOW_MS)

    async def send_audio(
        self, ws: aiohttp.ClientWebSocketResponse, audio: InputStream[Any], fmt: AudioFormat,
        limit: int,
    ) -> None:  # fmt: skip
        sample = fmt.bytes_per_sample
        frame = fmt.sample_rate * FRAME_MS // 1000 * sample
        buffer = b""
        async for chunk in audio:
            buffer += chunk.pcm
            while len(buffer) >= frame:
                await ws.send_bytes(buffer[:frame])
                buffer = buffer[frame:]
                self._samples += frame // sample
            if self._samples >= limit:
                self._turn.trace("deepgram.stt.limit", seconds=self._samples / fmt.sample_rate)
                break
        whole = len(buffer) - len(buffer) % sample
        if whole:
            await ws.send_bytes(buffer[:whole])
            self._samples += whole // sample
        await ws.send_str(json.dumps({"type": "ForceEndTurn"}))

    async def receive(self, ws: aiohttp.ClientWebSocketResponse, sender: asyncio.Task[None]) -> str:
        """Read results until the command is over; return what ended it.

        One read stays pending across iterations; a failed audio input (or send) ends the turn
        with its error, and once the audio is over Flux must end the turn in time.
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
                        continue  # the audio ended; wait for the end of the turn
                    raise DeepgramError(f"Flux did not end the turn within {FINAL_TIMEOUT_S:.0f}s")
                msg, read = read.result(), None
                if (ended := await self._handle(ws, msg, sender.done())) is not None:
                    return ended
        finally:
            if read is not None:
                read.cancel()

    async def _handle(
        self, ws: aiohttp.ClientWebSocketResponse, msg: aiohttp.WSMessage, audio_over: bool
    ) -> str | None:
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            if audio_over:
                return "closed"  # nothing was said: Flux closed after CloseStream
            raise DeepgramError(f"Deepgram closed the connection: {msg.extra or ws.close_code}")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise DeepgramError(f"connection failed: {ws.exception()}")
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        data = json.loads(msg.data)
        if (err := error_of(data)) is not None:
            raise err
        if data.get("type") == "Warning":
            self._turn.trace("deepgram.stt.warning", code=data.get("code"))
            return "no_speech" if data.get("code") == NO_ACTIVE_TURN else None
        if data.get("type") != "TurnInfo":
            return None
        self._words = _tokens(data)
        if languages := data.get("languages"):
            self.language = str(languages[0])
        if (words := data.get("words")) and words[-1].get("end") is not None:
            self._speech_end_s = float(words[-1]["end"])
        if data.get("event") == "EndOfTurn":
            self._turns += 1
            if self._only_wake_word() and not audio_over:
                self._words = []
                return None  # the wake word alone: keep listening for the command
            return str(data.get("trigger") or "model")
        await self._show_partial()
        return None

    def _only_wake_word(self) -> bool:
        text, found = self._strip(self._words)
        return found and not text

    async def _show_partial(self) -> None:
        text = self._strip(self._words)[0]
        if text and text != self._partial_shown:
            self._partial_shown = text
            await self._turn.output("transcript").send(
                Transcript(text, final=False, language=self.language)
            )

    def stats(self, ended_by: str, fmt: AudioFormat) -> dict[str, Any]:
        _, found = self._strip(self._words)
        return {
            "ended_by": ended_by,
            "chars": len(self.text),
            "audio_s": round(self._samples / fmt.sample_rate, 2),
            "speech_end_s": round(self._speech_end_s, 2),
            "turns": self._turns,
            "wake_word_cut": found,
        }


def _tokens(data: Mapping[str, Any]) -> list[Token]:
    """The turn's words as pieces for the wake word cut; the transcript if words are missing."""
    words = data.get("words") or []
    if not words:
        transcript = str(data.get("transcript") or "")
        return [Token(" " + transcript, 0)] if transcript else []
    return [
        Token(" " + str(w.get("word", "")), round(1000 * float(w.get("start") or 0))) for w in words
    ]
