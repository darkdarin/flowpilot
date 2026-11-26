"""``soniox.stt``: streaming speech-to-text over the Soniox real-time WebSocket API.

The turn's audio is sent as it comes; partial results go out on ``transcript`` while the user
speaks. Soniox detects the end of the command itself (``<end>``): the node then closes its audio
input, so the satellite stops streaming, and sends the final text. If the audio ends first, the
node asks Soniox to finalize what it heard (``<fin>``).

Some satellites send audio from before the command, wake word included; with ``wake_words`` set
the node cuts the wake word (and anything before it) from the text. An endpoint right after the
wake word alone does not end the command.

A dialogue keeps its connection (``keep_connection_s``): the next command of the session goes on
the same stream, so Soniox hears the dialogue and the connection is ready. Between turns the
node's keeper task sends keepalives, finalizes and drops what Soniox still holds from the last
command (audio after its end must not start the next one) and closes a connection idle that long.
Soniox bills the time a stream is open.

Before a turn's audio the node tells Soniox the assistant's last answer (``agent_text``): Soniox
then knows what the person replies to ("в спальне", "да") and leaves an echo of the assistant out
of the text; the assistant's words never come back in the transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
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
    dialogue,
    node_translations,
)
from flowpilot_sdk.wake_word import Token, strip_wake_word

from .common import (
    API_KEY_SETTING,
    LANGUAGES,
    REPLACED_MODELS,
    STT_MODEL,
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
KEEP_CONNECTION_S = 30.0
CLOSE_TIMEOUT_S = 1.0  # closing within a turn: a cancelled one must stop soon
KEEPALIVE_S = 8.0  # Soniox closes a stream after 20 s without a message
AGENT_TEXT_RECENT_S = 60.0  # an older answer is not what the person replies to
AGENT_TEXT_MAX_CHARS = 1000  # the end of a long answer: where its question is

_LOGGER = logging.getLogger(__name__)


def takes_agent_text(model: str) -> bool:
    """Whether a Soniox model accepts ``agent_text`` (stt-rt-v6 and newer)."""
    found = re.match(r"stt-rt-v(\d+)", model)
    return found is not None and int(found.group(1)) >= 6


def answer_tail(text: str) -> str:
    """The end of an answer, at most ``AGENT_TEXT_MAX_CHARS``, starting at a word."""
    text = text.strip()
    if len(text) <= AGENT_TEXT_MAX_CHARS:
        return text
    tail = text[-AGENT_TEXT_MAX_CHARS:]
    return tail.split(" ", 1)[1] if " " in tail else tail


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
                    "description": f"A real-time Soniox model; {STT_MODEL} when not set.",
                    "type": "string",
                    "default": STT_MODEL,
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
                "keep_connection_s": {
                    "title": "Keep the connection, s",
                    "description": (
                        "After a command the connection stays open this long, so the next one in "
                        "the dialogue goes on it; 0 opens one per command. Soniox bills the time "
                        "it is open; 30 when not set."
                    ),
                    "type": "number",
                    "minimum": 0,
                    "maximum": 300,
                    "default": KEEP_CONNECTION_S,
                },
                "agent_text": {
                    "title": "Tell Soniox the answer",
                    "description": (
                        "Soniox hears what the assistant answered last: it knows what the person "
                        "replies to and leaves an echo of the assistant out of the text "
                        "(stt-rt-v6 and newer); on when not set."
                    ),
                    "type": "boolean",
                    "default": True,
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
        self._http: aiohttp.ClientSession | None = None
        # the dialogues' connections by session id; closed ones wait for the keeper under
        # another key
        self._streams: dict[str, _Stream] = {}
        self._keeper: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

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
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
        )
        self._keeper = asyncio.create_task(self._keep(), name="soniox stt connections")

    async def stop(self) -> None:
        if self._keeper is not None:
            self._keeper.cancel()
            await asyncio.gather(self._keeper, return_exceptions=True)
            self._keeper = None
        streams, self._streams = list(self._streams.values()), {}
        for stream in streams:
            await stream.close()
        if self._http is not None:
            await self._http.close()
            self._http = None

    @property
    def _detects(self) -> bool:
        return bool(self._config.get("detect_language", True))

    def _session_config(self, fmt: AudioFormat, language: str | None) -> dict[str, Any]:
        cfg = self._config
        model = str(cfg.get("model") or STT_MODEL)
        model = REPLACED_MODELS.get(model, model)
        config: dict[str, Any] = {
            "model": model,
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
        if cfg.get("agent_text", True) and takes_agent_text(model):
            config["enable_agent_text_input"] = True
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
        stream = await self._stream_for(
            turn.session.session_id, self._session_config(fmt, language)
        )
        recognition = _Recognition(
            turn,
            list(self._config.get("wake_words") or []),
            language or turn.context.language,
            offset_ms=stream.audio_ms,
        )
        ended_by = "error"
        try:
            answer = await self._tell_answer(turn, stream)
            limit = int(float(self._config.get("max_utterance_s", 15)) * fmt.sample_rate)
            sender = asyncio.create_task(recognition.send_audio(stream, audio, fmt, limit))
            try:
                ended_by = await recognition.receive(stream.ws, sender)
            finally:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError, StreamError):
                    await sender
                stream.audio_ms += recognition.audio_ms(fmt)
        finally:
            # Soniox ended the command and the audio after its end stays with Soniox: the keeper
            # finalizes and drops it before the next turn
            stream.needs_flush = ended_by == "end"
            stream.dead = stream.dead or ended_by not in ("end", "finalize")
            if stream.dead or stream.keep_s <= 0:
                await self._drop(stream)  # not kept: closed with the turn
            self._release(stream)
        audio.close()  # Soniox decided the command is over: the satellite can stop streaming
        text = recognition.text
        heard = (recognition.language if self._detects else None) or language
        turn.trace(
            "soniox.stt",
            **recognition.stats(ended_by, fmt),
            language=heard,
            connection_turn=stream.turns,
            agent_text_chars=answer,
        )
        await turn.output("transcript").send(Transcript(text, final=True, language=heard))
        if text:
            await turn.output("utterance").send(Utterance(text, heard))

    async def _stream_for(self, session_id: str, config: dict[str, Any]) -> _Stream:
        """The dialogue's connection when it is open with the same settings, else a new one.
        A turn of a dialogue whose connection is in use gets one of its own."""
        key = json.dumps(config, sort_keys=True)
        keep = float(self._config.get("keep_connection_s", KEEP_CONNECTION_S))
        stream = self._streams.get(session_id) if keep > 0 else None
        # the dialogue's next connection is this turn's, unless another turn uses the current one
        takes_over = keep > 0 and (stream is None or not stream.busy)
        if stream is not None and not stream.busy:
            await stream.lock.acquire()
            stream.busy = True
            if not stream.dead and not stream.ws.closed and stream.config == key:
                try:
                    if stream.needs_flush:
                        await stream.flush()
                    stream.turns += 1
                    return stream
                except SonioxError, aiohttp.ClientError, ConnectionError, TimeoutError:
                    pass  # a new connection instead
                except BaseException:  # cancelled: the connection must not stay taken
                    stream.dead = True
                    self._release(stream)
                    raise
            stream.dead = True
            self._release(stream)
        assert self._http is not None, "the node is not started"
        ws = await self._http.ws_connect(
            STT_URL, headers={"Authorization": f"Bearer {self._api_key}"}, compress=0
        )
        fresh = _Stream(ws, key, keep, agent_text=bool(config.get("enable_agent_text_input")))
        await fresh.lock.acquire()
        fresh.busy = True
        try:
            await fresh.send(config)
        except BaseException:
            fresh.dead = True
            self._release(fresh)
            raise
        if takes_over:
            if stream is not None and self._streams.get(session_id) is stream:
                self._streams[f"closed:{id(stream)}"] = stream  # the keeper closes it
            self._streams[session_id] = fresh
        else:  # in use by another turn, or not kept: closed once released
            fresh.keep_s = 0.0
            self._streams[f"once:{id(fresh)}"] = fresh
        return fresh

    async def _drop(self, stream: _Stream) -> None:
        for key in [k for k, v in self._streams.items() if v is stream]:
            del self._streams[key]
        await stream.close(timeout_s=CLOSE_TIMEOUT_S)

    def _release(self, stream: _Stream) -> None:
        stream.busy = False
        stream.released_at = time.monotonic()
        if stream.lock.locked():
            stream.lock.release()
        self._wake.set()

    async def _tell_answer(self, turn: TurnSession, stream: _Stream) -> int:
        """Send the assistant's last answer of the dialogue, if new to this connection and
        recent; returns how many characters went."""
        if not stream.agent_text:
            return 0
        kept = await dialogue.exchanges(turn.shared)
        if not kept:
            return 0
        at = float(kept[-1].get("at") or 0)
        text = answer_tail(str(kept[-1].get("assistant") or ""))
        if not text or at <= stream.reply_at or time.time() - at > AGENT_TEXT_RECENT_S:
            return 0
        stream.reply_at = at
        await stream.send({"type": "agent_text", "text": text, "end_of_segment": True})
        return len(text)

    async def _keep(self) -> None:
        """Between turns: keepalives, the rest of a finished command dropped, idle connections
        closed (see the module's docstring)."""
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(KEEPALIVE_S / 2):
                    await self._wake.wait()
            self._wake.clear()
            for key, stream in list(self._streams.items()):
                if stream.busy or stream.lock.locked():
                    continue
                async with stream.lock:
                    try:
                        keep = await stream.tend()
                    except Exception:  # noqa: BLE001 - one bad connection must not stop the rest
                        _LOGGER.exception("Soniox connection upkeep failed")
                        keep = False
                    if not keep:
                        stream.dead = True
                        if self._streams.get(key) is stream:
                            del self._streams[key]
                        await stream.close()


class _Stream:
    """One WebSocket stream to Soniox, kept for a dialogue between its turns."""

    def __init__(
        self, ws: aiohttp.ClientWebSocketResponse, config: str, keep_s: float, *, agent_text: bool
    ) -> None:
        self.ws = ws
        self.config = config  # the session configuration it was opened with
        self.keep_s = keep_s
        self.agent_text = agent_text
        self.lock = asyncio.Lock()
        self.busy = False  # a turn uses it
        self.dead = False
        self.needs_flush = False
        self.audio_ms = 0  # audio sent so far: a turn's tokens are timed from here
        self.reply_at = 0.0  # when the answer sent last was given
        self.released_at = time.monotonic()
        self.sent_at = time.monotonic()
        self.turns = 1

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(message))
        self.sent_at = time.monotonic()

    async def send_audio(self, data: bytes) -> None:
        await self.ws.send_bytes(data)
        self.sent_at = time.monotonic()

    async def flush(self) -> None:
        """Finalize what Soniox still holds from the last command and drop it."""
        await self.send({"type": "finalize"})
        async with asyncio.timeout(FINAL_TIMEOUT_S):
            while True:
                data = _message(self.ws, await self.ws.receive())
                if data is None:
                    continue
                tokens = data.get("tokens", [])
                if any(t.get("is_final") and t.get("text") == FIN_TOKEN for t in tokens):
                    break
        self.needs_flush = False

    async def tend(self) -> bool:
        """Upkeep while no turn uses it; False when it is to be closed."""
        if self.dead or self.ws.closed:
            return False
        if time.monotonic() - self.released_at >= self.keep_s:
            return False
        try:
            if self.needs_flush:
                await self.flush()
            while True:  # what came while idle: only an error or the end is expected
                try:
                    msg = await self.ws.receive(timeout=0.01)
                except TimeoutError:
                    break
                _message(self.ws, msg)
            if time.monotonic() - self.sent_at >= KEEPALIVE_S:
                await self.send({"type": "keepalive"})
        except SonioxError, aiohttp.ClientError, ConnectionError, TimeoutError:
            return False
        return True

    async def close(self, timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        self.dead = True
        with contextlib.suppress(Exception):
            async with asyncio.timeout(timeout_s):
                await self.ws.close()


def _message(ws: aiohttp.ClientWebSocketResponse, msg: aiohttp.WSMessage) -> dict[str, Any] | None:
    """A Soniox message as data; a closed connection, a failure or an error raises."""
    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
        raise SonioxError("Soniox closed the connection")
    if msg.type == aiohttp.WSMsgType.ERROR:
        raise SonioxError(f"connection failed: {ws.exception()}")
    if msg.type != aiohttp.WSMsgType.TEXT:
        return None
    data: dict[str, Any] = json.loads(msg.data)
    if (err := error_of(data)) is not None:
        raise err
    if data.get("finished"):
        raise SonioxError("Soniox finished the stream")
    return data


class _Recognition:
    """The tokens of one turn and the WebSocket exchange that produces them."""

    def __init__(
        self, turn: TurnSession, wake_words: list[str], language: str | None, *, offset_ms: int = 0
    ) -> None:
        self._turn = turn
        self._offset_ms = offset_ms  # where the turn's audio starts in the stream
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

    def audio_ms(self, fmt: AudioFormat) -> int:
        return self._samples * 1000 // fmt.sample_rate

    async def send_audio(
        self, stream: _Stream, audio: InputStream[Any], fmt: AudioFormat, limit: int
    ) -> None:
        frame = fmt.bytes_per_sample
        rest = b""
        async for chunk in audio:
            data = rest + chunk.pcm
            whole = len(data) - len(data) % frame  # Soniox rejects partial samples
            data, rest = data[:whole], data[whole:]
            if data:
                await stream.send_audio(data)
                self._samples += len(data) // frame
            if self._samples >= limit:
                self._turn.trace("soniox.stt.limit", seconds=self._samples / fmt.sample_rate)
                break
        await stream.send({"type": "finalize"})

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
                partial.append(Token(text, self._ms(token, "start_ms")))
                continue
            if text == END_TOKEN:
                if self._only_wake_word():
                    continue  # the wake word alone: keep listening for the command
                return "end"
            if text == FIN_TOKEN:
                return "finalize"
            self._final.append(Token(text, self._ms(token, "start_ms")))
            if heard := token.get("language"):
                self._letters[str(heard)] += sum(c.isalpha() for c in text)
            if text.strip() and "end_ms" in token:
                self._speech_end_ms = self._ms(token, "end_ms")
        if data.get("finished"):
            return "finished"
        await self._show_partial(partial)
        return None

    def _ms(self, token: Mapping[str, Any], field: str) -> int:
        """A token's time from the start of the turn's audio."""
        return int(token.get(field, 0)) - self._offset_ms

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
