"""WebSocket server: clients start turns, stream audio and serve host operations (ADR-014)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import io
import json
import logging
import time
import wave
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

from aiohttp import WSCloseCode, WSMsgType, web

from flowpilot_core import GraphIssue, ValidatedGraph
from flowpilot_core.runtime import Deployment, TurnStatus
from flowpilot_sdk import (
    AudioFormat,
    DeviceInfo,
    Evidence,
    HostAudio,
    HostError,
    HostUnavailable,
    Principal,
    Response,
    SessionInfo,
    SourceInfo,
    TurnContext,
    new_ulid,
)

from . import __version__
from .editor_api import add_editor_routes
from .manager import GraphManager
from .nodes import ClientInput, ClientSink, ClientSource
from .protocol import (
    MAX_STREAM_ID,
    PROTOCOL_VERSION,
    TURN_PATH,
    WS_PATH,
    ProtocolError,
    pack_audio,
    unpack_audio,
)
from .references import (
    HOST_OP_OPTIONS,
    OPTIONS_CACHE_S,
    merge_options,
    reference_warnings,
    references,
)
from .tracehub import EDITOR_WS_PATH, TraceHub, editor_socket

_LOGGER = logging.getLogger(__name__)

SESSION_IDLE_S = 3600.0
OPTIONS_TIMEOUT_S = 5.0
AUDIO_QUEUE_CHUNKS = 64
MAX_HTTP_AUDIO_BYTES = 16 * 1024 * 1024


@dataclass
class _Session:
    info: SessionInfo
    last_used: float


class SessionIndex:
    """Maps external dialogue keys (e.g. an HA conversation id) to FlowPilot sessions."""

    def __init__(self, idle_s: float = SESSION_IDLE_S) -> None:
        self._idle_s = idle_s
        self._by_key: dict[tuple[str, str], _Session] = {}

    def resolve(self, external: Mapping[str, str]) -> SessionInfo:
        now = time.monotonic()
        self._by_key = {k: s for k, s in self._by_key.items() if now - s.last_used < self._idle_s}
        key = next(
            ((k, v) for k, v in sorted(external.items()) if k.endswith("conversation_id")), None
        )
        if key is None:
            return SessionInfo(new_ulid(), datetime.now(UTC), 0, dict(external))
        existing = self._by_key.get(key)
        if existing is None:
            info = SessionInfo(new_ulid(), datetime.now(UTC), 0, dict(external))
        else:
            prev = existing.info
            info = SessionInfo(
                prev.session_id, prev.created_at, prev.turn_index + 1, dict(external)
            )
        self._by_key[key] = _Session(info, now)
        return info


def client_graph_issues(graph: ValidatedGraph) -> list[GraphIssue]:
    """A served graph starts at ``client.source`` and has exactly one ``client.sink``."""
    issues = []
    if graph.specs[graph.source].type != ClientSource.spec.type:
        issues.append(
            GraphIssue("client_source", "$.nodes", f"input must be {ClientSource.spec.type}")
        )
    sinks = [n for n, s in graph.specs.items() if s.type == ClientSink.spec.type]
    if len(sinks) != 1:
        issues.append(
            GraphIssue("client_sink", "$.nodes", f"needs exactly one {ClientSink.spec.type}")
        )
    return issues


def source_of(deployment: Deployment) -> ClientSource:
    return cast(ClientSource, deployment.instance(deployment.graph.source))


def sink_of(deployment: Deployment) -> ClientSink:
    node_id = next(n for n, s in deployment.graph.specs.items() if s.type == ClientSink.spec.type)
    return cast(ClientSink, deployment.instance(node_id))


EDITOR_NOT_BUILT = "The editor is not built: run `npm ci && npm run build` in editor/."


def editor_file(path: str, root: Path | None = None) -> Path | None:
    """A file of the built editor inside the package, ``index.html`` for app routes."""
    root = (root or Path(str(resources.files("flowpilot_server").joinpath("editor")))).resolve()
    index = root / "index.html"
    if not index.is_file():
        return None
    target = (root / path).resolve()
    return target if target.is_relative_to(root) and target.is_file() else index


class FlowPilotServer:
    """Serves deployed graphs to clients over the FlowPilot protocol."""

    def __init__(
        self,
        deployments: Mapping[str, Deployment] | None = None,
        *,
        token: str,
        default_graph: str,
        manager: GraphManager | None = None,
    ) -> None:
        self.manager = manager or GraphManager.adopt(deployments or {})
        self.manager.set_check(client_graph_issues)
        for graph_id, d in self.manager.deployments.items():
            if issues := client_graph_issues(d.graph):
                raise ValueError(f"graph {graph_id!r}: {issues[0].message}")
        if self.manager.store is None and default_graph not in self.manager.deployments:
            raise ValueError(f"default graph {default_graph!r} is not deployed")
        self.default_graph = default_graph
        self._token = token
        self.sessions = SessionIndex()
        self._sockets: set[web.WebSocketResponse] = set()
        self.trace_hub = TraceHub()
        self.manager.set_trace(self.trace_hub.sink, self._on_deployed)
        self.connections: set[Connection] = set()
        self._options_cache: tuple[float, dict[str, Any]] | None = None
        self._background: set[asyncio.Task[None]] = set()

    def _on_deployed(self, graph_id: str) -> None:
        self.trace_hub.announce(graph_id, "deployed")
        self._spawn(self.broadcast_references())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- host options and references ------------------------------------------------------------

    def deployed_references(self) -> dict[str, list[dict[str, Any]]]:
        registry = self.manager.registry
        return {
            graph_id: [r.as_dict() for r in references(d.graph.graph.to_dict(), registry)]
            for graph_id, d in self.deployments.items()
        }

    async def broadcast_references(self) -> None:
        """Tell hosts which of their entities the deployed graphs use (repair issues in HA)."""
        msg = {"type": "references", "graphs": self.deployed_references()}
        for conn in list(self.connections):
            if conn.offers(HOST_OP_OPTIONS):
                with contextlib.suppress(ConnectionError, HostUnavailable):
                    await conn.send(msg)

    async def host_options(self, *, max_age_s: float = OPTIONS_CACHE_S) -> dict[str, Any]:
        """What connected hosts offer for marked settings; cached briefly."""
        now = time.monotonic()
        if self._options_cache is not None and now - self._options_cache[0] < max_age_s:
            return self._options_cache[1]
        hosts = [c for c in list(self.connections) if c.offers(HOST_OP_OPTIONS)]

        async def ask(conn: Connection) -> Any:
            async with asyncio.timeout(OPTIONS_TIMEOUT_S):
                return await conn.call(None, HOST_OP_OPTIONS, {})

        results = await asyncio.gather(*(ask(c) for c in hosts), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, BaseException)]
        for conn, r in zip(hosts, results, strict=True):
            if isinstance(r, BaseException):
                _LOGGER.warning("host %s did not list options: %s", conn.client.get("name"), r)
        options, strict = merge_options(ok)
        value = {
            "available": bool(ok),
            "hosts": [str(c.client.get("name") or "client") for c in hosts],
            "options": options,
            "strict": strict,
        }
        self._options_cache = (now, value)
        return value

    async def reference_warnings(self, data: Any) -> list[dict[str, Any]]:
        options = await self.host_options()
        if not options["available"]:
            return []
        return reference_warnings(
            data, self.manager.registry, options["options"], options["strict"]
        )

    def track_socket(self, ws: web.WebSocketResponse) -> None:
        self._sockets.add(ws)

    def untrack_socket(self, ws: web.WebSocketResponse) -> None:
        self._sockets.discard(ws)

    @property
    def deployments(self) -> Mapping[str, Deployment]:
        return self.manager.deployments

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_HTTP_AUDIO_BYTES)
        app.router.add_get(WS_PATH, self._handle_ws)
        app.router.add_post(TURN_PATH, self._handle_http_turn)
        app.router.add_get("/", self._handle_static)
        app.router.add_get("/app.js", self._handle_static)
        app.on_shutdown.append(self._close_sockets)
        app.router.add_get(EDITOR_WS_PATH, self._handle_editor_ws)
        app.router.add_get("/editor", self._handle_editor_root)
        app.router.add_get("/editor/{path:.*}", self._handle_editor_file)
        add_editor_routes(app, self)
        return app

    async def _handle_editor_ws(self, request: web.Request) -> web.WebSocketResponse:
        return await editor_socket(request, self)

    async def _handle_editor_root(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/editor/")

    async def _handle_editor_file(self, request: web.Request) -> web.StreamResponse:
        """The built graph editor (``npm run build`` in ``editor/``); SPA fallback to index."""
        target = editor_file(request.match_info["path"])
        if target is None:
            return web.Response(status=404, text=EDITOR_NOT_BUILT)
        cache = "no-cache" if target.name == "index.html" else "public, max-age=31536000, immutable"
        return web.FileResponse(target, headers={"Cache-Control": cache})

    async def _handle_static(self, request: web.Request) -> web.Response:
        """Development client page: microphone and text input against the protocol."""
        name = "app.js" if request.path == "/app.js" else "index.html"
        content_type = "text/javascript" if name == "app.js" else "text/html"
        body = resources.files("flowpilot_server").joinpath("static", name).read_text()
        return web.Response(text=body, content_type=content_type)

    async def _handle_http_turn(self, request: web.Request) -> web.Response:
        """One turn over plain HTTP: JSON text or a WAV body in, transcript and response out."""
        auth = request.headers.get("Authorization", "")
        if not self.check_token(auth.removeprefix("Bearer ").strip()):
            return web.json_response({"error": "invalid token"}, status=401)
        graph_id = request.query.get("graph") or self.default_graph
        if graph_id not in self.deployments:
            return web.json_response({"error": f"unknown graph {graph_id!r}"}, status=404)
        language = request.query.get("language")
        conversation_id = request.query.get("conversation_id")
        inp = ClientInput()
        if request.content_type == "application/json":
            body = await request.json()
            inp.text = str(body["text"])
            language = body.get("language", language)
            conversation_id = body.get("conversation_id", conversation_id)
        elif request.content_type in ("audio/wav", "audio/x-wav", "audio/wave"):
            try:
                fmt, pcm = _read_wav(await request.read())
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            inp.audio_format, inp.audio = fmt, asyncio.Queue()
            step = fmt.sample_rate * fmt.bytes_per_sample // 50  # 20 ms
            for i in range(0, len(pcm), step):
                inp.audio.put_nowait(pcm[i : i + step])
            inp.audio.put_nowait(None)
        else:
            return web.json_response(
                {"error": "send application/json {text} or audio/wav"}, status=415
            )

        ctx, session = self.new_turn(
            graph_id,
            adapter_id="http",
            source={"kind": "http"},
            language=language,
            external={"http.conversation_id": conversation_id} if conversation_id else {},
        )
        outlet = _Collector()
        with self.manager.turn(graph_id) as deployment:
            source_of(deployment).inputs[ctx.turn_id] = inp
            sink_of(deployment).outlets[ctx.turn_id] = outlet
            try:
                result = await deployment.run_turn(ctx, session)
            finally:
                source_of(deployment).inputs.pop(ctx.turn_id, None)
                sink_of(deployment).outlets.pop(ctx.turn_id, None)
        reply: dict[str, Any] = {
            "turn_id": ctx.turn_id,
            "session_id": session.session_id,
            "status": result.status.value,
            "nodes": {n: o.status.value for n, o in result.nodes.items()},
            "partials": outlet.partials,
            "transcript": outlet.final,
            "response": outlet.answer,
            "speech": None,
        }
        if outlet.speech_format is not None:
            speech: dict[str, Any] = {
                "format": outlet.speech_format.as_meta(),
                "bytes": len(outlet.speech),
            }
            reply["speech"] = speech
            if request.query.get("speech") == "wav":
                speech["wav_base64"] = base64.b64encode(
                    _wav(outlet.speech_format, bytes(outlet.speech))
                ).decode()
        return web.json_response(reply)

    async def _close_sockets(self, app: web.Application) -> None:
        """Close client connections on shutdown instead of waiting for clients to hang up."""
        for ws in list(self._sockets):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"FlowPilot is shutting down")

    def required_host_ops(self, graph_id: str) -> set[str]:
        return {op for s in self.deployments[graph_id].graph.specs.values() for op in s.host_ops}

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        conn = Connection(self, ws)
        try:
            await conn.run()
        finally:
            self._sockets.discard(ws)
            self.connections.discard(conn)
            self._options_cache = None
            await conn.close()
        return ws

    def new_turn(
        self,
        graph_id: str,
        *,
        adapter_id: str,
        source: Mapping[str, Any],
        language: str | None,
        external: Mapping[str, str],
        user_id: Any = None,
    ) -> tuple[TurnContext, SessionInfo]:
        """Context and session for a new turn on ``graph_id``."""
        session = self.sessions.resolve(external)
        principal = (
            Principal.unknown(Evidence("client_user", {"user_id": str(user_id)}))
            if user_id
            else Principal.unknown()
        )
        timeout_s = self.deployments[graph_id].graph.graph.settings.turn_timeout_ms / 1000
        ctx = TurnContext(
            turn_id=new_ulid(),
            session_id=session.session_id,
            started_at=datetime.now(UTC),
            deadline_mono_ns=time.monotonic_ns() + int(timeout_s * 1e9),
            source=SourceInfo(kind=str(source.get("kind", "client")), adapter_id=adapter_id),
            trace_id=new_ulid(),
            initial_principal=principal,
            device=DeviceInfo(
                device_id=source.get("device_id"),
                satellite_id=source.get("satellite_id"),
                area=source.get("area"),
            ),
            language=language,
            external=dict(external),
        )
        return ctx, session

    def check_token(self, token: Any) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self._token)


def _read_wav(data: bytes) -> tuple[AudioFormat, bytes]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise ValueError("WAV must be 16-bit PCM")
            fmt = AudioFormat(sample_rate=w.getframerate(), channels=w.getnchannels())
            return fmt, w.readframes(w.getnframes())
    except (wave.Error, EOFError) as err:
        raise ValueError(f"not a WAV file: {err}") from err


def _wav(fmt: AudioFormat, pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(fmt.channels)
        w.setsampwidth(2)
        w.setframerate(fmt.sample_rate)
        w.writeframes(pcm)
    return out.getvalue()


class _Collector:
    """Collects one turn's results for an HTTP reply."""

    def __init__(self) -> None:
        self.partials: list[str] = []
        self.final: str | None = None
        self.answer: dict[str, Any] | None = None
        self.speech_format: AudioFormat | None = None
        self.speech = bytearray()

    async def transcript(self, text: str, final: bool) -> None:
        if final:
            self.final = text
        else:
            self.partials.append(text)

    async def delta(self, text: str) -> None:
        return None

    async def response(self, response: Response) -> None:
        self.answer = {
            "text": response.text,
            "speech_text": response.speech_text,
            "language": response.language,
            "continue_conversation": response.continue_conversation,
            "data": dict(response.data) if response.data else None,
        }

    async def speech_start(self, fmt: AudioFormat) -> None:
        self.speech_format = fmt

    async def speech_chunk(self, pcm: bytes) -> None:
        self.speech.extend(pcm)

    async def speech_end(self) -> None:
        return None


class _Outlet:
    """Sends one turn's results to the client."""

    def __init__(self, conn: Connection, turn_id: str) -> None:
        self._conn = conn
        self._turn_id = turn_id
        self._stream: int | None = None

    async def transcript(self, text: str, final: bool) -> None:
        await self._conn.send(
            {"type": "turn.transcript", "turn_id": self._turn_id, "text": text, "final": final}
        )

    async def delta(self, text: str) -> None:
        await self._conn.send({"type": "turn.delta", "turn_id": self._turn_id, "text": text})

    async def response(self, response: Response) -> None:
        await self._conn.send(
            {
                "type": "turn.response",
                "turn_id": self._turn_id,
                "response": {
                    "text": response.text,
                    "speech_text": response.speech_text,
                    "language": response.language,
                    "continue_conversation": response.continue_conversation,
                    "data": dict(response.data) if response.data else None,
                },
            }
        )

    async def speech_start(self, fmt: AudioFormat) -> None:
        self._stream = self._conn.next_stream_id()
        await self._conn.send(
            {
                "type": "stream.start",
                "turn_id": self._turn_id,
                "stream": self._stream,
                "kind": "speech",
                "format": fmt.as_meta(),
            }
        )

    async def speech_chunk(self, pcm: bytes) -> None:
        assert self._stream is not None
        await self._conn.send_bytes(pack_audio(self._stream, pcm))

    async def speech_end(self) -> None:
        if self._stream is not None:
            await self._conn.send({"type": "stream.end", "stream": self._stream})
            self._stream = None


class _HostAudio:
    """Audio streamed back by the client for one host call (``HostAudio``)."""

    def __init__(self, result: asyncio.Future[Any]) -> None:
        self._result = result
        self.format: AudioFormat | None = None
        self.started: asyncio.Future[AudioFormat] = asyncio.get_running_loop().create_future()
        # Unbounded: the consumer forwards speech straight into the graph.
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finished = False

    def finish(self) -> None:
        if not self._finished:
            self._finished = True
            self.queue.put_nowait(None)

    async def _iter(self) -> AsyncIterator[bytes]:
        if self.format is None:
            return
        while (pcm := await self.queue.get()) is not None:
            yield pcm

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def result(self) -> Any:
        return await self._result


class Connection:
    """One client connection; also the ``HostCaller`` for the turns it starts."""

    def __init__(self, server: FlowPilotServer, ws: web.WebSocketResponse) -> None:
        self.server = server
        self.ws = ws
        self.client: dict[str, Any] = {}
        self.host_ops: frozenset[str] = frozenset()
        self._send_lock = asyncio.Lock()
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._in_streams: dict[int, tuple[str, asyncio.Queue[bytes | None]]] = {}
        self._calls: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._host_audio: dict[int, _HostAudio] = {}  # by call id
        self._client_audio: dict[int, _HostAudio] = {}  # by client stream id
        self._next_call = 0
        self._next_stream = 0
        self._authed = False

    # --- HostCaller -----------------------------------------------------------------------------

    def offers(self, op: str) -> bool:
        return op in self.host_ops

    def _register_call(self, op: str) -> tuple[int, asyncio.Future[Any]]:
        if self.ws.closed:
            raise HostUnavailable(op, "client disconnected")
        self._next_call += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._calls[self._next_call] = (op, fut)
        return self._next_call, fut

    async def call(
        self,
        turn_id: str | None,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any:
        call_id, fut = self._register_call(op)
        body = dict(args)
        stream_id: int | None = None
        if audio_in is not None:
            assert audio_format is not None
            stream_id = self.next_stream_id()
            body["audio"] = {"stream": stream_id, "format": audio_format.as_meta()}
        pump: asyncio.Task[None] | None = None
        try:
            await self.send(
                {"type": "host.call", "call": call_id, "turn_id": turn_id, "op": op, "args": body}
            )
            if audio_in is None or stream_id is None:
                return await fut
            pump = asyncio.create_task(self._pump_audio(stream_id, audio_in))
            done, _ = await asyncio.wait({fut, pump}, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()  # the host may answer before all audio was sent
            pump.result()  # raise if the audio input failed
            return await fut
        finally:
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            self._calls.pop(call_id, None)

    async def _pump_audio(self, stream_id: int, audio_in: AsyncIterable[bytes]) -> None:
        try:
            async for pcm in audio_in:
                await self.send_bytes(pack_audio(stream_id, pcm))
        finally:
            await self.send({"type": "stream.end", "stream": stream_id})

    @contextlib.asynccontextmanager
    async def stream(
        self, turn_id: str, op: str, args: Mapping[str, Any]
    ) -> AsyncIterator[HostAudio]:
        call_id, fut = self._register_call(op)
        audio = _HostAudio(fut)
        self._host_audio[call_id] = audio
        try:
            await self.send(
                {"type": "host.call", "call": call_id, "turn_id": turn_id, "op": op, "args": args}
            )
            # Wait until the host starts streaming audio, or answers without any.
            await asyncio.wait({audio.started, fut}, return_when=asyncio.FIRST_COMPLETED)
            if audio.started.done():
                audio.format = audio.started.result()
            else:
                fut.result()  # raises HostError if the operation failed
                audio.finish()
            yield audio
        finally:
            self._host_audio.pop(call_id, None)
            for stream_id, entry in list(self._client_audio.items()):
                if entry is audio:
                    del self._client_audio[stream_id]
            self._calls.pop(call_id, None)

    # --- sending --------------------------------------------------------------------------------

    async def send(self, msg: Mapping[str, Any]) -> None:
        async with self._send_lock:
            if not self.ws.closed:
                await self.ws.send_str(json.dumps(msg))

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            if not self.ws.closed:
                await self.ws.send_bytes(data)

    def next_stream_id(self) -> int:
        self._next_stream = self._next_stream % MAX_STREAM_ID + 1
        return self._next_stream

    # --- receiving ------------------------------------------------------------------------------

    async def run(self) -> None:
        async for msg in self.ws:
            try:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(json.loads(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    await self._on_binary(msg.data)
            except ProtocolError as err:
                payload: dict[str, Any] = {
                    "type": "error",
                    "code": err.code,
                    "message": err.message,
                }
                if err.ref is not None:
                    payload["ref"] = err.ref
                await self.send(payload)
                if err.code == "auth":
                    await self.ws.close()
            except (ValueError, TypeError, KeyError) as err:
                await self.send({"type": "error", "code": "bad_message", "message": str(err)})

    async def _on_text(self, msg: Any) -> None:
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ProtocolError("bad_message", "expected a JSON object with a type")
        kind = msg["type"]
        if not self._authed:
            if kind != "hello":
                raise ProtocolError("auth", "hello required first")
            await self._on_hello(msg)
            return
        if kind == "turn.start":
            await self._on_turn_start(msg)
        elif kind == "stream.start":
            self._on_host_stream_start(msg)
        elif kind == "stream.end":
            stream_id = int(msg["stream"])
            entry = self._in_streams.pop(stream_id, None)
            if entry is not None:
                await entry[1].put(None)
            elif (audio := self._client_audio.pop(stream_id, None)) is not None:
                audio.finish()
        elif kind == "turn.cancel":
            task = self._turns.get(str(msg["turn_id"]))
            if task is not None:
                task.cancel()
        elif kind == "host.result":
            call = self._calls.get(int(msg["call"]))
            if call is not None and not call[1].done():
                op, fut = call
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(HostError(op, str(msg.get("error", "unknown error"))))
                audio = self._host_audio.get(int(msg["call"]))
                if audio is not None:
                    audio.finish()  # no more audio once the result is in
        else:
            raise ProtocolError("unknown_type", f"unknown message type {kind!r}")

    async def _on_hello(self, msg: dict[str, Any]) -> None:
        if msg.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError("auth", f"unsupported protocol {msg.get('protocol')!r}")
        if not self.server.check_token(msg.get("token")):
            raise ProtocolError("auth", "invalid token")
        self._authed = True
        self.client = dict(msg.get("client") or {})
        self.host_ops = frozenset(str(op) for op in msg.get("host_ops") or [])
        missing = {
            g: sorted(self.server.required_host_ops(g) - self.host_ops)
            for g in self.server.deployments
        }
        await self.send(
            {
                "type": "hello.ok",
                "protocol": PROTOCOL_VERSION,
                "server": {"version": __version__},
                "graphs": sorted(self.server.deployments),
                "default_graph": self.server.default_graph,
                "missing_host_ops": {g: ops for g, ops in missing.items() if ops},
            }
        )
        self.server.connections.add(self)
        self.server._options_cache = None
        if self.offers(HOST_OP_OPTIONS):
            await self.send({"type": "references", "graphs": self.server.deployed_references()})

    def _on_host_stream_start(self, msg: dict[str, Any]) -> None:
        """The client starts streaming the audio result of a host call."""
        audio = self._host_audio.get(int(msg["call"]))
        if audio is None:
            raise ProtocolError("unknown_call", f"no pending host call {msg['call']}")
        stream_id = int(msg["stream"])
        if stream_id in self._in_streams or stream_id in self._client_audio:
            raise ProtocolError("stream_in_use", f"stream {stream_id} is already open")
        self._client_audio[stream_id] = audio
        if not audio.started.done():
            audio.started.set_result(AudioFormat.from_meta(msg["format"]))

    async def _on_binary(self, data: bytes) -> None:
        stream, pcm = unpack_audio(data)
        entry = self._in_streams.get(stream)
        if entry is not None:
            await entry[1].put(pcm)  # blocks when the graph lags: TCP backpressure to the client
            return
        audio = self._client_audio.get(stream)
        if audio is None:
            raise ProtocolError("unknown_stream", f"no open client stream {stream}")
        audio.queue.put_nowait(pcm)

    async def _on_turn_start(self, msg: dict[str, Any]) -> None:
        ref = msg.get("ref")
        ref = str(ref) if ref is not None else None
        graph_id = str(msg.get("graph") or self.server.default_graph)
        if graph_id not in self.server.deployments:
            raise ProtocolError("unknown_graph", f"graph {graph_id!r} is not deployed", ref)

        raw_input = msg.get("input")
        if not isinstance(raw_input, dict) or not raw_input:
            raise ProtocolError("bad_input", "input must contain audio and/or utterance", ref)
        inp = ClientInput()
        audio_stream: int | None = None
        if "utterance" in raw_input:
            inp.text = str(raw_input["utterance"]["text"])
        if "audio" in raw_input:
            audio_stream = int(raw_input["audio"]["stream"])
            if audio_stream in self._in_streams:
                raise ProtocolError("stream_in_use", f"stream {audio_stream} is already open", ref)
            inp.audio_format = AudioFormat.from_meta(raw_input["audio"]["format"])
            inp.audio = asyncio.Queue(AUDIO_QUEUE_CHUNKS)

        ctx, session = self.server.new_turn(
            graph_id,
            adapter_id=str(self.client.get("name", "client")),
            source=msg.get("source") or {},
            language=msg.get("language"),
            external={str(k): str(v) for k, v in (msg.get("external") or {}).items()},
            user_id=msg.get("user_id"),
        )

        if audio_stream is not None:
            assert inp.audio is not None
            self._in_streams[audio_stream] = (ctx.turn_id, inp.audio)
        await self.send(
            {
                "type": "turn.started",
                "ref": ref,
                "turn_id": ctx.turn_id,
                "session_id": session.session_id,
                "graph": graph_id,
            }
        )
        task = asyncio.create_task(
            self._run_turn(graph_id, inp, ctx, session), name=f"turn:{ctx.turn_id}"
        )
        self._turns[ctx.turn_id] = task

    async def _run_turn(
        self, graph_id: str, inp: ClientInput, ctx: TurnContext, session: SessionInfo
    ) -> None:
        status, nodes = "cancelled", {}
        try:
            # The turn stays on this deployment even if the graph is redeployed meanwhile.
            with self.server.manager.turn(graph_id) as deployment:
                source_of(deployment).inputs[ctx.turn_id] = inp
                sink_of(deployment).outlets[ctx.turn_id] = _Outlet(self, ctx.turn_id)
                try:
                    result = await deployment.run_turn(ctx, session, host=self)
                finally:
                    source_of(deployment).inputs.pop(ctx.turn_id, None)
                    sink_of(deployment).outlets.pop(ctx.turn_id, None)
            status = result.status.value
            nodes = {n: o.status.value for n, o in result.nodes.items()}
            if result.status is not TurnStatus.OK:
                _LOGGER.info("turn %s finished %s: %s", ctx.turn_id, status, nodes)
        except asyncio.CancelledError:
            status = "cancelled"
        finally:
            self._turns.pop(ctx.turn_id, None)
            for stream, (turn_id, _) in list(self._in_streams.items()):
                if turn_id == ctx.turn_id:
                    del self._in_streams[stream]
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.send(
                    {"type": "turn.end", "turn_id": ctx.turn_id, "status": status, "nodes": nodes}
                )

    async def close(self) -> None:
        for audio in self._host_audio.values():
            audio.finish()
        for op, fut in self._calls.values():
            if not fut.done():
                fut.set_exception(HostUnavailable(op, "client disconnected"))
        for _, queue in self._in_streams.values():
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        tasks = list(self._turns.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
