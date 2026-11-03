"""WebSocket server: clients start turns, stream audio and serve host operations (ADR-014)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from aiohttp import WSMsgType, web

from flowpilot_core.runtime import Deployment, TurnStatus
from flowpilot_sdk import (
    AudioFormat,
    DeviceInfo,
    Evidence,
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
from .nodes import ClientInput, ClientSink, ClientSource
from .protocol import (
    MAX_STREAM_ID,
    PROTOCOL_VERSION,
    WS_PATH,
    ProtocolError,
    pack_audio,
    unpack_audio,
)

_LOGGER = logging.getLogger(__name__)

SESSION_IDLE_S = 3600.0
AUDIO_QUEUE_CHUNKS = 64


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


class FlowPilotServer:
    """Serves deployed graphs to clients over the FlowPilot protocol."""

    def __init__(
        self, deployments: Mapping[str, Deployment], *, token: str, default_graph: str
    ) -> None:
        if default_graph not in deployments:
            raise ValueError(f"default graph {default_graph!r} is not deployed")
        self.deployments = dict(deployments)
        self.default_graph = default_graph
        self._token = token
        self.sessions = SessionIndex()
        self._sinks: dict[str, str] = {}
        for graph_id, d in self.deployments.items():
            g = d.graph
            if g.specs[g.source].type != ClientSource.spec.type:
                raise ValueError(f"graph {graph_id!r}: input must be {ClientSource.spec.type}")
            sinks = [n for n, s in g.specs.items() if s.type == ClientSink.spec.type]
            if len(sinks) != 1:
                raise ValueError(f"graph {graph_id!r}: needs exactly one {ClientSink.spec.type}")
            self._sinks[graph_id] = sinks[0]

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(WS_PATH, self._handle_ws)
        return app

    def required_host_ops(self, graph_id: str) -> set[str]:
        return {op for s in self.deployments[graph_id].graph.specs.values() for op in s.host_ops}

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        conn = Connection(self, ws)
        try:
            await conn.run()
        finally:
            await conn.close()
        return ws

    def check_token(self, token: Any) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self._token)

    def source(self, graph_id: str) -> ClientSource:
        d = self.deployments[graph_id]
        return cast(ClientSource, d.instance(d.graph.source))

    def sink(self, graph_id: str) -> ClientSink:
        return cast(ClientSink, self.deployments[graph_id].instance(self._sinks[graph_id]))


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
        self._next_call = 0
        self._next_stream = 0
        self._authed = False

    # --- HostCaller -----------------------------------------------------------------------------

    def offers(self, op: str) -> bool:
        return op in self.host_ops

    async def call(self, turn_id: str, op: str, args: Mapping[str, Any]) -> Any:
        if self.ws.closed:
            raise HostUnavailable(op, "client disconnected")
        self._next_call += 1
        call_id = self._next_call
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._calls[call_id] = (op, fut)
        try:
            await self.send(
                {"type": "host.call", "call": call_id, "turn_id": turn_id, "op": op, "args": args}
            )
            return await fut
        finally:
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
        elif kind == "stream.end":
            entry = self._in_streams.pop(int(msg["stream"]), None)
            if entry is not None:
                await entry[1].put(None)
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

    async def _on_binary(self, data: bytes) -> None:
        stream, pcm = unpack_audio(data)
        entry = self._in_streams.get(stream)
        if entry is None:
            raise ProtocolError("unknown_stream", f"no open client stream {stream}")
        await entry[1].put(pcm)  # blocks when the graph lags: TCP backpressure to the client

    async def _on_turn_start(self, msg: dict[str, Any]) -> None:
        ref = msg.get("ref")
        ref = str(ref) if ref is not None else None
        graph_id = str(msg.get("graph") or self.server.default_graph)
        if graph_id not in self.server.deployments:
            raise ProtocolError("unknown_graph", f"graph {graph_id!r} is not deployed", ref)
        deployment = self.server.deployments[graph_id]

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

        external = {str(k): str(v) for k, v in (msg.get("external") or {}).items()}
        session = self.server.sessions.resolve(external)
        source = msg.get("source") or {}
        user_id = msg.get("user_id")
        principal = (
            Principal.unknown(Evidence("client_user", {"user_id": str(user_id)}))
            if user_id
            else Principal.unknown()
        )
        timeout_s = deployment.graph.graph.settings.turn_timeout_ms / 1000
        ctx = TurnContext(
            turn_id=new_ulid(),
            session_id=session.session_id,
            started_at=datetime.now(UTC),
            deadline_mono_ns=time.monotonic_ns() + int(timeout_s * 1e9),
            source=SourceInfo(
                kind=str(source.get("kind", "client")),
                adapter_id=str(self.client.get("name", "client")),
            ),
            trace_id=new_ulid(),
            initial_principal=principal,
            device=DeviceInfo(
                device_id=source.get("device_id"),
                satellite_id=source.get("satellite_id"),
                area=source.get("area"),
            ),
            language=msg.get("language"),
            external=external,
        )

        self.server.source(graph_id).inputs[ctx.turn_id] = inp
        self.server.sink(graph_id).outlets[ctx.turn_id] = _Outlet(self, ctx.turn_id)
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
            self._run_turn(graph_id, deployment, ctx, session), name=f"turn:{ctx.turn_id}"
        )
        self._turns[ctx.turn_id] = task

    async def _run_turn(
        self, graph_id: str, deployment: Deployment, ctx: TurnContext, session: SessionInfo
    ) -> None:
        status, nodes = "cancelled", {}
        try:
            result = await deployment.run_turn(ctx, session, host=self)
            status = result.status.value
            nodes = {n: o.status.value for n, o in result.nodes.items()}
            if result.status is not TurnStatus.OK:
                _LOGGER.info("turn %s finished %s: %s", ctx.turn_id, status, nodes)
        except asyncio.CancelledError:
            status = "cancelled"
        finally:
            self._turns.pop(ctx.turn_id, None)
            self.server.source(graph_id).inputs.pop(ctx.turn_id, None)
            self.server.sink(graph_id).outlets.pop(ctx.turn_id, None)
            for stream, (turn_id, _) in list(self._in_streams.items()):
                if turn_id == ctx.turn_id:
                    del self._in_streams[stream]
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.send(
                    {"type": "turn.end", "turn_id": ctx.turn_id, "status": status, "nodes": nodes}
                )

    async def close(self) -> None:
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
