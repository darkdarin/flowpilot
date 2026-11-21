"""``llm.voice``: a voice agent on a speech-to-speech model (the OpenAI Realtime API).

The person's speech goes to the model as it is heard and the model answers with its own voice: no
separate speech recognition and text-to-speech, so the answer starts sooner and sounds more alive.
The agent knows what the LLM agent knows (the dialogue, the home's people, facts from the Memory
page) and uses the same tools: Home Assistant's, skills, memory and web search, executed by
FlowPilot with the speaker's rights.

One turn is one Realtime session over a WebSocket:

1. The connection, the turn's context (facts, people, Home Assistant's tools and state) and the
   first audio come in parallel: audio is kept until the session is set up, then sent on as it
   comes, resampled to the model's 24 kHz.
2. The model's voice activity detection hears the end of the command (``semantic_vad``); the
   audio input is then closed so the satellite stops streaming. The end of the audio stream (the
   satellite's own detection) or the longest command ends it too.
3. The answer's audio goes out on ``speech`` as the model speaks, its words on ``delta``; tool
   calls are run between responses, up to ``MAX_TOOL_ROUNDS``; the last round must answer.

The dialogue keeps what the person said (the model's own transcription) and the answer, shared
with the LLM agent, so a chat and a voice satellite continue the same conversation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import time
from array import array
from collections.abc import Callable, Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

import aiohttp

from flowpilot_sdk import (
    DYNAMIC_KEY,
    ENUM_TITLES_KEY,
    GROUPS_KEY,
    NODE_OPTIONS_KEY,
    AudioChunk,
    AudioFormat,
    ConfigOption,
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    Response,
    StreamError,
    TextDelta,
    Transcript,
    TurnSession,
    dialogue,
    node_translations,
)
from flowpilot_sdk.language import LANGUAGE_META, language_base

from .agent import (
    DEFAULT_HISTORY_HOURS,
    HOME_CALL_OP,
    HOME_TOOLS_OP,
    MAX_TOOL_ROUNDS,
    PEOPLE_OP,
    SEARCH_TEXT,
    SERVICE_CALL_OP,
    STATE_TEXT,
    Journal,
    LlmAgent,
    LlmError,
    _memory_text,
    _people_text,
    _tool_detail,
    _tools_text,
)
from .tools import Toolbox

REALTIME_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_VOICE = "marin"
VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]
DEFAULT_TRANSCRIPTION = "gpt-4o-mini-transcribe"
RATE = 24000  # the Realtime API's PCM, in and out
CONNECT_TIMEOUT_S = 10.0
TRANSCRIPT_WAIT_S = 3.0  # the person's words may come after the answer
COMMIT_EMPTY = "input_audio_buffer_commit_empty"
DEFAULT_PAUSE_MS = 600
SILENCE_GRACE_S = 0.7  # after the audio ends, for the model's last voice activity events

VOICE_TEXT = (
    "You speak aloud through a smart speaker. Answer briefly and naturally, as a person would "
    "say it: one or two sentences unless asked for more, no lists, no Markdown, no addresses of "
    "sites. Answer in the language the person speaks."
)
HISTORY_TEXT = "Earlier in this conversation (oldest first):\n{}"


class VoiceError(LlmError):
    pass


class Resampler:
    """Streaming linear resampling of PCM s16le to mono at another rate; keeps its place between
    chunks, so a stream cut into pieces sounds the same as a whole one."""

    def __init__(self, source: int, target: int, channels: int = 1) -> None:
        self._step = source / target
        self._same = source == target
        self._channels = channels
        self._last: int | None = None
        self._at = 0.0  # where the next output sample is, in input samples (``_last`` is 0)
        self._rest = b""

    def convert(self, pcm: bytes) -> bytes:
        pcm = self._rest + pcm
        width = 2 * self._channels
        cut = len(pcm) - len(pcm) % width
        pcm, self._rest = pcm[:cut], pcm[cut:]
        values = array("h", pcm)
        if self._channels > 1:
            n = self._channels
            values = array("h", (sum(values[i : i + n]) // n for i in range(0, len(values), n)))
        if self._same:
            return values.tobytes()
        buf = values if self._last is None else array("h", [self._last]) + values
        if len(buf) < 2:
            if buf:
                self._last = buf[-1]
            return b""
        out = array("h")
        at, step, last = self._at, self._step, len(buf) - 1
        while at < last:
            i = int(at)
            frac = at - i
            out.append(round(buf[i] + (buf[i + 1] - buf[i]) * frac))
            at += step
        self._at = at - last
        self._last = buf[-1]
        return out.tobytes()


def _models_url(url: str) -> str | None:
    """``…/v1/models`` of a Realtime address on OpenAI's pattern (``wss://host/v1/realtime``)."""
    parts = urlsplit(url)
    if not parts.path.rstrip("/").endswith("/realtime"):
        return None
    scheme = "https" if parts.scheme in ("wss", "https") else "http"
    base = parts.path.rstrip("/").removesuffix("/realtime")
    return f"{scheme}://{parts.netloc}{base}/models"


def _session_url(url: str, model: str) -> str:
    if "model=" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}model={model}"


def _realtime_tool(definition: Mapping[str, Any]) -> dict[str, Any]:
    """A Chat Completions function definition in the Realtime API's flat form."""
    function = definition.get("function") or {}
    return {
        "type": "function",
        "name": function.get("name"),
        "description": function.get("description") or "",
        "parameters": function.get("parameters") or {"type": "object"},
    }


SHARED_SETTINGS = (
    "api_key",
    "system_prompt",
    "history_turns",
    "history_hours",
    "memory",
    "home",
    "home_state",
    "people",
    "remember",
    "web_search",
    "web_search_key",
    "web_search_url",
    "web_search_results",
    "timeout_s",
)


def _config_schema() -> dict[str, Any]:
    agent = LlmAgent.spec.config_schema
    shared = {k: copy.deepcopy(agent["properties"][k]) for k in SHARED_SETTINGS}
    groups = copy.deepcopy(agent[GROUPS_KEY])
    groups["tools"]["description"] = (
        "What the agent may do itself: control the home, search the web."
    )
    shared["api_key"]["description"] = "A runtime secret: the key of the Realtime API."
    shared["timeout_s"]["description"] = (
        "The longest a turn may wait for the answer after the command; 60 when not set."
    )
    return {
        "type": "object",
        GROUPS_KEY: groups,
        "properties": {
            "url": {
                "title": "Realtime address",
                "description": (
                    "The WebSocket address of a Realtime API: OpenAI's, or a compatible one (for "
                    "example Yandex AI Studio's, with the model in the address)."
                ),
                "type": "string",
                "default": REALTIME_URL,
            },
            "api_key": shared.pop("api_key"),
            "model": {
                "title": "Model",
                "description": (
                    f"The speech-to-speech model; {DEFAULT_MODEL} when not set (cheaper), "
                    "gpt-realtime-2.1 answers better."
                ),
                "type": "string",
                "default": DEFAULT_MODEL,
                NODE_OPTIONS_KEY: True,
            },
            "voice": {
                "title": "Voice",
                "description": (
                    f"The model's voice; {DEFAULT_VOICE} when not set. OpenAI recommends marin "
                    "and cedar."
                ),
                "type": "string",
                "default": DEFAULT_VOICE,
                NODE_OPTIONS_KEY: True,
                DYNAMIC_KEY: True,
            },
            "eagerness": {
                "title": "End of command",
                "description": (
                    "How the model hears that the person has finished. By a pause: after "
                    "Pause, ms of silence, quick and steady. By meaning: the model judges whether "
                    "the phrase is complete, so a slow speaker is not cut off, but a short "
                    "command may wait seconds for its timeout (Soon waits least, Late most). By "
                    "a pause when not set."
                ),
                "enum": ["pause", "auto", "high", "medium", "low"],
                ENUM_TITLES_KEY: {
                    "pause": "By a pause",
                    "auto": "By meaning, automatic",
                    "high": "By meaning, soon",
                    "medium": "By meaning, medium",
                    "low": "By meaning, late",
                },
                "default": "pause",
            },
            "pause_ms": {
                "title": "Pause, ms",
                "description": (
                    f"Silence after speech that ends the command (By a pause); {DEFAULT_PAUSE_MS}."
                    " Less answers sooner but may cut in on a pause for thought."
                ),
                "type": "integer",
                "minimum": 200,
                "maximum": 3000,
                "default": DEFAULT_PAUSE_MS,
            },
            "transcription": {
                "title": "Transcription model",
                "description": (
                    "Writes down what the person said, for chats, the dialogue and debug; "
                    f"{DEFAULT_TRANSCRIPTION} when not set."
                ),
                "type": "string",
                "default": DEFAULT_TRANSCRIPTION,
            },
            "max_utterance_s": {
                "title": "Longest command, s",
                "description": "The command ends here even if the person goes on; 15.",
                "type": "number",
                "minimum": 3,
                "maximum": 60,
                "default": 15,
            },
            **shared,
        },
        "additionalProperties": False,
    }


class VoiceAgent(LlmAgent):
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="llm.voice",
        translations=node_translations(__package__, "llm.voice"),
        version="1.0.0",
        title="Voice agent",
        category="agent",
        inputs={
            "audio": PortSpec("audio.pcm/1", description="The person's speech, as it is heard."),
            "principal": PortSpec(
                "identity.principal/1",
                kind="value",
                required=False,
                description=(
                    "Who speaks: the client input's, or an identity node's. Not connected: an "
                    "unknown person (no personal memory)."
                ),
            ),
        },
        outputs={
            "speech": PortSpec(
                "audio.pcm/1", description="The answer in the model's voice, as it speaks."
            ),
            "transcript": PortSpec(
                "text.transcript/1",
                description="What the person said, as the model wrote it down (for chats).",
            ),
            "delta": PortSpec("text.delta/1", description="The words of the answer as it speaks."),
            "response": PortSpec(
                "agent.response/1", kind="value", description="The whole answer, for the client."
            ),
        },
        config_schema=_config_schema(),
        optional_host_ops=frozenset({PEOPLE_OP, HOME_TOOLS_OP, HOME_CALL_OP, SERVICE_CALL_OP}),
        turn_timeout_s=310.0,
        description=(
            "Hears and answers with one speech-to-speech model (OpenAI Realtime API or a "
            "compatible one): no separate recognition and synthesis, so it answers sooner, with "
            "the same memory and tools as the LLM agent."
        ),
    )

    @classmethod
    async def config_options(cls, field: str, config: Mapping[str, Any]) -> list[ConfigOption]:
        if field == "voice":
            return [ConfigOption(v) for v in VOICES]
        if field != "model":
            return []
        url = _models_url(str(config.get("url") or REALTIME_URL))
        key = config.get("api_key")
        if url is None or not isinstance(key, str):
            return []
        timeout = aiohttp.ClientTimeout(total=15)
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.get(url, headers={"Authorization": f"Bearer {key}"}) as resp,
        ):
            if resp.status != 200:
                return []
            data = await resp.json()
        ids = sorted(str(m.get("id")) for m in data.get("data") or [] if m.get("id"))
        return [ConfigOption(m) for m in ids if "realtime" in m]

    async def start(self, ctx: NodeContext) -> None:
        self._skills = getattr(ctx, "skills", None)
        self._config = dict(ctx.config)
        self._node = ctx.node_id
        self._memory = getattr(ctx, "memory", None)
        ref = ctx.config.get("api_key")
        self._key = ctx.secret(ref["secret"]) if isinstance(ref, dict) else None
        self._start_search(ctx)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
        self._http = aiohttp.ClientSession(timeout=timeout)

    def _turn_detection(self) -> dict[str, Any]:
        """How the model hears the end of the command; the agent asks for the answer itself once
        it is committed, so never twice."""
        mode = str(self._config.get("eagerness") or "pause")
        common = {"create_response": False, "interrupt_response": False}
        if mode == "pause":
            pause = int(self._config.get("pause_ms", DEFAULT_PAUSE_MS))
            return {"type": "server_vad", "silence_duration_ms": pause} | common
        return {"type": "semantic_vad", "eagerness": mode} | common

    async def _instructions(
        self, turn: TurnSession, cfg: Mapping[str, Any], language: str | None, log: Journal
    ) -> tuple[str, Toolbox]:
        """The session's instructions and tools for this turn."""
        speaker = await self._speaker(turn)
        home, own = self._known(speaker)
        toolbox = self._toolbox(speaker)
        personal = self._config.get("memory", "personal") == "personal"
        guide = (
            _tools_text(speaker is not None or not personal, auto=self._remember() == "auto")
            if toolbox
            else ""
        )
        keep = int(self._config.get("history_turns", 10))
        quiet_s = 3600.0 * int(self._config.get("history_hours", DEFAULT_HISTORY_HOURS))
        earlier = dialogue.recent(await self._dialogue(turn), turns=keep, quiet_s=quiet_s)
        lines = [
            f"{role}: {text}"
            for e in (earlier[-keep:] if keep else [])
            for role, text in (("User", e.get("user")), ("You", e.get("assistant")))
            if text
        ]
        done: list[dict[str, Any]] = []
        said: list[str] = []
        people, (home_prompt, home_tools, home_state) = await asyncio.gather(
            self._people(turn), self._home(turn, speaker, language, done, said)
        )
        if home_tools:
            toolbox = toolbox.with_tools(home_tools)
        if self._search is not None and self._http is not None:
            toolbox = toolbox.with_tools([self._search.tool(self._http, language)])
        elif self._search_problem:
            turn.trace("llm.voice.no_web_search", reason=self._search_problem)
        who = next(
            (str(p.get("name")) for p in people or [] if speaker and p.get("user_id") == speaker),
            speaker,
        )
        parts = (
            self._system(language, cfg),
            VOICE_TEXT,
            _people_text(people, speaker),
            _memory_text(home, own),
            home_prompt if home_tools else "",
            guide,
            SEARCH_TEXT if self._search is not None else "",
            HISTORY_TEXT.format("\n".join(lines)) if lines else "",
            STATE_TEXT.format(home_state) if home_tools and home_state else "",
        )
        instructions = "\n\n".join(p for p in parts if p)
        log(
            "context",
            f"speaker: {who or 'unknown'}; {len(home)} facts of the home, {len(own)} own; "
            f"dialogue: {len(lines) // 2} earlier exchanges; tools: {len(toolbox)} "
            f"({len(home_tools)} from Home Assistant)",
            instructions or None,
            tools=len(toolbox),
        )
        return instructions, toolbox

    async def run_turn(self, turn: TurnSession) -> None:
        audio: InputStream[Any] = turn.input("audio")
        header = await audio.header()
        if header is None:
            return  # a text turn: the LLM agent's
        assert self._http is not None
        fmt = AudioFormat.from_meta(header.meta)
        cfg = await turn.settings(self._config)  # instructions or voice for this turn
        language = turn.context.language
        model = str(self._config.get("model") or DEFAULT_MODEL)
        log = Journal(turn)
        started = time.monotonic()
        heard: asyncio.Queue[bytes | None] = asyncio.Queue()
        resampler = Resampler(fmt.sample_rate, RATE, fmt.channels)
        limit = int(float(self._config.get("max_utterance_s", 15)) * fmt.sample_rate)
        ended_by = "stream"

        async def listen() -> None:
            """The satellite's audio, resampled, until it ends or the longest command."""
            nonlocal ended_by
            samples = 0
            try:
                async for chunk in audio:
                    if pcm := resampler.convert(chunk.pcm):
                        heard.put_nowait(pcm)
                    samples += len(chunk.pcm) // fmt.bytes_per_sample
                    if samples >= limit:
                        ended_by = "limit"
                        break
            except StreamError:
                pass
            finally:
                heard.put_nowait(None)

        listener = asyncio.create_task(listen())
        connecting = asyncio.create_task(
            self._http.ws_connect(
                _session_url(str(self._config.get("url") or REALTIME_URL), model),
                headers={"Authorization": f"Bearer {self._key}"} if self._key else None,
                heartbeat=None,
                max_msg_size=0,
            )
        )
        # who speaks, facts and tools come in parallel: the audio does not wait for them
        context = asyncio.create_task(self._instructions(turn, cfg, language, log))
        sender: asyncio.Task[None] | None = None
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            try:
                ws = await connecting
            except aiohttp.ClientError as err:
                raise VoiceError(f"the Realtime API is unreachable: {err}") from err
            base = language_base(language)
            session: dict[str, Any] = {
                "type": "realtime",
                "model": model,
                "instructions": VOICE_TEXT,  # the turn's own come with its context
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": RATE},
                        "transcription": {
                            "model": str(self._config.get("transcription") or DEFAULT_TRANSCRIPTION)
                        }
                        | ({"language": base} if base else {}),
                        "turn_detection": self._turn_detection(),
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": RATE},
                        "voice": str(cfg.get("voice") or DEFAULT_VOICE),
                    },
                },
            }
            await ws.send_json({"type": "session.update", "session": session})
            exchange = _Exchange(turn, ws, context, log, language, audio)
            exchange.audio_end = lambda: ended_by
            sender = asyncio.create_task(exchange.send_audio(heard))
            await exchange.run(sender, timeout_s=float(self._config.get("timeout_s", 60)))
        finally:
            for task in (listener, sender, connecting, context):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            if ws is None and connecting.done() and not connecting.cancelled():
                ws = connecting.result() if connecting.exception() is None else None
            if ws is not None:
                await ws.close()
        await exchange.finish(turn, started, model, self._node)


class _Exchange:
    """One turn's Realtime events: the audio out, the person's words, the answer and tools."""

    def __init__(
        self,
        turn: TurnSession,
        ws: aiohttp.ClientWebSocketResponse,
        context: asyncio.Task[tuple[str, Toolbox]],
        log: Journal,
        language: str | None,
        audio: InputStream[Any],
    ) -> None:
        self._turn = turn
        self._ws = ws
        self._context = context
        self._toolbox = Toolbox([])
        self._context_sent = False
        self._log = log
        self._language = language
        self._audio = audio
        self.audio_end: Callable[[], str] = lambda: "stream"  # why the audio ended
        self.speech_started = False
        self.committed = False
        self.ended_by: str | None = None
        self.user_text = ""
        self._user_done = asyncio.Event()
        self.pieces: list[str] = []
        self._new_round = False  # tools ran: keep the next round's words apart
        self._speech_offset = 0
        self._speech_open = False
        self._delta_open = False
        self.committed_at: float | None = None
        self.first_audio_ms: int | None = None
        self.used: list[str] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.heard_s = 0.0

    async def send_audio(self, heard: asyncio.Queue[bytes | None]) -> None:
        """Audio to the model until it ends or the command is over."""
        while (pcm := await heard.get()) is not None:
            if self.committed:
                return
            self.heard_s += len(pcm) / 2 / RATE
            await self._ws.send_json(
                {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
            )

    async def _send_context(self) -> None:
        """The turn's instructions and tools, once (waits for them): before any answer."""
        if self._context_sent:
            return
        instructions, self._toolbox = await self._context
        session: dict[str, Any] = {"type": "realtime", "instructions": instructions}
        if self._toolbox:
            session["tools"] = [_realtime_tool(d) for d in self._toolbox.definitions()]
            session["tool_choice"] = "auto"
        await self._ws.send_json({"type": "session.update", "session": session})
        self._context_sent = True

    async def run(self, sender: asyncio.Task[None], *, timeout_s: float) -> None:
        """Read events until the answer is done (or nothing was said)."""
        rounds = 0
        deadline: float | None = None
        audio_over: float | None = None  # when all the audio was sent, the command not over
        asked_commit = False
        while True:
            if not self._context_sent and self._context.done():
                await self._send_context()
            if sender.done() and not sender.cancelled() and (err := sender.exception()):
                raise VoiceError(f"sending the audio failed: {err}") from err
            if sender.done() and not self.committed and not asked_commit:
                # the audio ended before the model heard the end: once its last events are in,
                # ask for the answer if it heard speech; silence alone is no command
                audio_over = audio_over or time.monotonic()
                if self.speech_started:
                    asked_commit = True
                    self.ended_by = self.audio_end()
                    await self._ws.send_json({"type": "input_audio_buffer.commit"})
                elif time.monotonic() - audio_over >= SILENCE_GRACE_S:
                    self.ended_by = "silence"
                    return
            wait = 0.1 if not self.committed else None
            if deadline is not None:
                wait = max(0.0, deadline - time.monotonic())
                if wait == 0.0:
                    raise VoiceError(f"no answer in {timeout_s:g} s")
            try:
                msg = await asyncio.wait_for(self._ws.receive(), wait)
            except TimeoutError:
                continue
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                raise VoiceError(f"the Realtime API closed the session: {msg.extra or ''}")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise VoiceError(f"the Realtime session failed: {self._ws.exception()}")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(msg.data)
            kind = event.get("type")
            if kind == "input_audio_buffer.speech_started":
                self.speech_started = True
            elif kind == "input_audio_buffer.committed":
                if self.committed:
                    continue
                self.committed = True
                self.committed_at = time.monotonic()
                self.ended_by = self.ended_by or "model"
                self._audio.close()  # the command is over: the satellite can stop streaming
                deadline = time.monotonic() + timeout_s
                await self._send_context()
                await self._ws.send_json({"type": "response.create"})
            elif kind == "conversation.item.input_audio_transcription.completed":
                await self._heard(str(event.get("transcript") or ""))
            elif kind == "conversation.item.input_audio_transcription.failed":
                self._turn.trace("llm.voice.transcription_failed", reason=str(event.get("error")))
                self._user_done.set()
            elif kind == "response.output_audio.delta":
                await self._speak(base64.b64decode(event.get("delta") or ""))
            elif kind == "response.output_audio_transcript.delta":
                await self._say(str(event.get("delta") or ""))
            elif kind == "response.done":
                response = event.get("response") or {}
                usage = response.get("usage") or {}
                for key in self.usage:
                    self.usage[key] += int(usage.get(key) or 0)
                status = response.get("status")
                if status == "failed":
                    details = response.get("status_details") or {}
                    error = details.get("error") or details
                    raise VoiceError(f"the model failed: {error}")
                calls = [
                    item
                    for item in response.get("output") or []
                    if item.get("type") == "function_call"
                ]
                if not calls or status != "completed":
                    return
                rounds += 1
                await self._run_tools(calls)
                last = rounds >= MAX_TOOL_ROUNDS
                create: dict[str, Any] = {"type": "response.create"}
                if last:  # the last round must answer in words
                    create["response"] = {"tool_choice": "none"}
                await self._ws.send_json(create)
                deadline = time.monotonic() + timeout_s
            elif kind == "error":
                error = event.get("error") or {}
                if error.get("code") == COMMIT_EMPTY:
                    if not self.committed:
                        self.ended_by = "silence"
                        return
                    continue  # our commit raced the model's: the model's counts
                raise VoiceError(f"the Realtime API refused: {error.get('message') or error}")

    async def _heard(self, text: str) -> None:
        self.user_text = " ".join(text.split())
        self._user_done.set()
        out = self._turn.output("transcript")
        if self.user_text:
            await out.send(Transcript(self.user_text, final=True, language=self._language))
        self._log("heard", self.user_text or "(nothing)")

    async def _speak(self, pcm: bytes) -> None:
        if not pcm:
            return
        speech = self._turn.output("speech")
        if not self._speech_open:
            await speech.open(AudioFormat(sample_rate=RATE).as_meta())
            self._speech_open = True
            if self.committed_at is not None:
                self.first_audio_ms = round(1000 * (time.monotonic() - self.committed_at))
        await speech.send(AudioChunk(pcm, self._speech_offset))
        self._speech_offset += len(pcm) // 2

    async def _say(self, text: str) -> None:
        if not text:
            return
        delta = self._turn.output("delta")
        if not self._delta_open:
            await delta.open({LANGUAGE_META: self._language} if self._language else None)
            self._delta_open = True
        if self.pieces and not self.pieces[-1][-1:].isspace() and self._new_round:
            text = " " + text if not text[:1].isspace() else text
        self._new_round = False
        self.pieces.append(text)
        await delta.send(TextDelta(text))

    async def _run_tools(self, calls: list[Mapping[str, Any]]) -> None:
        for call in calls:
            name = str(call.get("name") or "")
            arguments = str(call.get("arguments") or "")
            called_at = time.monotonic()
            result, ok = await self._toolbox.call(name, arguments)
            self.used.append(name if ok else f"{name}!")
            self._turn.trace("llm.voice.tool", tool=name, ok=ok)
            self._log(
                "tool",
                f"{name}: {'ok' if ok else 'failed'}",
                _tool_detail(arguments, result),
                tool=name,
                ok=ok,
                ms=round(1000 * (time.monotonic() - called_at)),
            )
            await self._ws.send_json(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": result,
                    },
                }
            )
        self._new_round = True

    async def finish(self, turn: TurnSession, started: float, model: str, node: str) -> None:
        """Close the streams; the dialogue and the response once the person's words are in."""
        if self.committed and not self._user_done.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._user_done.wait(), TRANSCRIPT_WAIT_S)
        await turn.output("speech").end()
        await turn.output("delta").end()
        answer = "".join(self.pieces).strip()
        turn.trace(
            "llm.voice",
            model=model,
            ended_by=self.ended_by,
            heard_s=round(self.heard_s, 2),
            first_audio_ms=self.first_audio_ms,
            total_ms=round(1000 * (time.monotonic() - started)),
            chars=len(answer),
            tools=",".join(self.used) or None,
            input_tokens=self.usage["input_tokens"],
            output_tokens=self.usage["output_tokens"],
        )
        if not answer:
            return  # nothing was said, or nothing answered: the turn ends quietly
        if self.user_text:
            await dialogue.add(turn.shared, self.user_text, answer, by=node)
        await turn.output("response").send(Response(text=answer, language=self._language))
