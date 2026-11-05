"""Client of the FlowPilot runtime protocol v1 (see flowpilot_server/protocol.py, ADR-014).

The integration ships without external requirements, so the small protocol client lives here.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import struct
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONNECT_TIMEOUT_S,
    PROTOCOL_VERSION,
    START_TIMEOUT_S,
    WS_PATH,
)

if TYPE_CHECKING:
    from homeassistant.components.conversation import ConversationInput

_LOGGER = logging.getLogger(__name__)
_STREAM_ID = struct.Struct(">I")

HostHandler = Callable[["HostRequest"], Awaitable[Any]]


class FlowPilotError(Exception):
    """The runtime is unreachable or rejected a request."""


class HostRequest:
    """One host operation requested by the runtime.

    ``audio()`` yields the PCM the runtime streams in (speech-to-text); ``send_audio()`` streams PCM
    back (text-to-speech). Both are injectable so handlers can be tested without a connection.
    """

    def __init__(
        self,
        handle: TurnHandle | None,
        args: Mapping[str, Any],
        *,
        audio: AsyncIterator[bytes] | None = None,
        send_audio: Callable[[Mapping[str, Any], AsyncIterable[bytes]], Awaitable[None]]
        | None = None,
    ) -> None:
        self.handle = handle
        self.args = args
        self._audio = audio
        self._send_audio = send_audio

    def audio(self) -> AsyncIterator[bytes]:
        if self._audio is None:
            raise FlowPilotError("this operation carries no audio")
        return self._audio

    async def send_audio(self, fmt: Mapping[str, Any], chunks: AsyncIterable[bytes]) -> None:
        if self._send_audio is None:
            raise FlowPilotError("this operation cannot stream audio back")
        await self._send_audio(fmt, chunks)


class TurnHandle:
    """One FlowPilot turn as seen from Home Assistant."""

    def __init__(
        self,
        client: FlowPilotClient,
        turn_id: str,
        session_id: str,
        conversation_id: str | None,
        audio_stream: int | None,
    ) -> None:
        self.client = client
        self.turn_id = turn_id
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.audio_stream = audio_stream
        self.graph: str | None = None  # as requested; None is the runtime's default graph
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.attached: asyncio.Future[ConversationInput] = (
            asyncio.get_running_loop().create_future()
        )
        self._audio_open = audio_stream is not None

    async def send_audio(self, pcm: bytes) -> None:
        if self._audio_open and self.audio_stream is not None:
            await self.client.send_bytes(_STREAM_ID.pack(self.audio_stream) + pcm)

    async def end_audio(self) -> None:
        if self._audio_open:
            self._audio_open = False
            await self.client.send({"type": "stream.end", "stream": self.audio_stream})

    async def cancel(self) -> None:
        await self.client.send({"type": "turn.cancel", "turn_id": self.turn_id})

    @callback
    def attach(self, user_input: ConversationInput) -> None:
        """Bind the conversation entity's input (device, satellite, user context) to this turn."""
        if not self.attached.done():
            self.attached.set_result(user_input)

    async def next_event(self, within: float) -> dict[str, Any]:
        async with asyncio.timeout(within):
            return await self.events.get()


class FlowPilotClient:
    """Keeps one WebSocket to the FlowPilot runtime, reconnecting as needed."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        token: str,
        *,
        signal: str,
        host_ops: Mapping[str, HostHandler] | None = None,
    ) -> None:
        self.hass = hass
        self.url = url.rstrip("/") + WS_PATH
        self._token = token
        self._signal = signal
        self.graphs_signal = f"{signal}_graphs"  # dispatched when the graph list changes
        self.host_ops: dict[str, HostHandler] = dict(host_ops or {})
        self.server_info: dict[str, Any] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._refs = itertools.count(1)
        self._streams = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._handles: dict[str, TurnHandle] = {}
        self._host_tasks: set[asyncio.Task[None]] = set()
        self._host_in: dict[int, asyncio.Queue[bytes | None]] = {}
        # Called with ``{graph: [{node, field, kind, id}]}`` whenever the runtime reports what its
        # deployed graphs use from Home Assistant.
        self.on_references: Callable[[dict[str, list[dict[str, Any]]]], None] | None = None
        # Deployed graphs (id -> name) and the default one, as the runtime last reported them.
        self.graphs: dict[str, str] = {}
        self.default_graph: str | None = None
        self.graphs_known = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # --- lifecycle ------------------------------------------------------------------------------

    async def async_validate(self) -> dict[str, Any]:
        """Connect once, perform the hello handshake and disconnect (config flow)."""
        session = async_get_clientsession(self.hass)
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            ws = await session.ws_connect(self.url)
            try:
                return await self._hello(ws)
            finally:
                await ws.close()

    def start(self) -> None:
        self._task = self.hass.async_create_background_task(self._run(), "flowpilot client")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()

    async def _run(self) -> None:
        backoff = 1.0
        session = async_get_clientsession(self.hass)
        while True:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
                    ws = await session.ws_connect(self.url, heartbeat=30)
                self.server_info = await self._hello(ws)
                self._ws = ws
                self._set_connected(True)
                _LOGGER.info("Connected to FlowPilot at %s", self.url)
                missing = self.server_info.get("missing_host_ops") or {}
                if missing:
                    _LOGGER.warning("FlowPilot graphs need unsupported operations: %s", missing)
                backoff = 1.0
                await self._read(ws)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, FlowPilotError, OSError) as err:
                _LOGGER.debug("FlowPilot connection failed: %s", err)
            finally:
                self._on_disconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _hello(self, ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        await ws.send_json(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "token": self._token,
                "client": {"name": "home-assistant"},
                "host_ops": sorted(self.host_ops),
            }
        )
        reply = await ws.receive_json(timeout=CONNECT_TIMEOUT_S)
        if reply.get("type") != "hello.ok":
            raise FlowPilotError(f"handshake rejected: {reply.get('message', reply)}")
        return dict(reply)

    def _set_connected(self, connected: bool) -> None:
        if connected:
            self._connected.set()
        else:
            self._connected.clear()
        async_dispatcher_send(self.hass, self._signal, connected)

    def _on_disconnect(self) -> None:
        was_connected = self.connected
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(FlowPilotError("disconnected"))
        self._pending.clear()
        for handle in self._handles.values():
            handle.events.put_nowait({"type": "turn.end", "status": "disconnected"})
        self._handles.clear()
        for queue in self._host_in.values():
            queue.put_nowait(None)
        self._host_in.clear()
        if was_connected:
            _LOGGER.warning("Disconnected from FlowPilot")
            self._set_connected(False)

    # --- sending --------------------------------------------------------------------------------

    async def send(self, msg: Mapping[str, Any]) -> None:
        async with self._send_lock:
            if self._ws is None or self._ws.closed:
                raise FlowPilotError("not connected")
            await self._ws.send_json(dict(msg))

    async def send_bytes(self, data: bytes) -> None:
        async with self._send_lock:
            if self._ws is None or self._ws.closed:
                raise FlowPilotError("not connected")
            await self._ws.send_bytes(data)

    async def start_turn(
        self,
        *,
        source: Mapping[str, Any],
        language: str | None,
        conversation_id: str | None,
        utterance: str | None = None,
        audio_format: Mapping[str, Any] | None = None,
        user_id: str | None = None,
        graph: str | None = None,
    ) -> TurnHandle:
        try:
            async with asyncio.timeout(START_TIMEOUT_S):
                await self._connected.wait()
        except TimeoutError as err:
            raise FlowPilotError("FlowPilot runtime is not connected") from err

        ref = str(next(self._refs))
        body: dict[str, Any] = {
            "type": "turn.start",
            "ref": ref,
            "source": dict(source),
            "language": language,
            "external": {"ha.conversation_id": conversation_id} if conversation_id else {},
            "input": {},
        }
        if user_id:
            body["user_id"] = user_id
        if graph is not None:  # None: the runtime's default graph
            body["graph"] = graph
        audio_stream: int | None = None
        if utterance is not None:
            body["input"]["utterance"] = {"text": utterance}
        if audio_format is not None:
            audio_stream = next(self._streams)
            body["input"]["audio"] = {"stream": audio_stream, "format": dict(audio_format)}

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ref] = fut
        try:
            await self.send(body)
            async with asyncio.timeout(START_TIMEOUT_S):
                reply = await fut
        finally:
            self._pending.pop(ref, None)
        if reply.get("type") != "turn.started":
            raise FlowPilotError(f"turn rejected: {reply.get('message', reply)}")
        handle = TurnHandle(
            self, reply["turn_id"], reply["session_id"], conversation_id, audio_stream
        )
        handle.graph = graph
        self._handles[handle.turn_id] = handle
        return handle

    # --- receiving ------------------------------------------------------------------------------

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self._dispatch(msg.json())
            elif msg.type == aiohttp.WSMsgType.BINARY:
                (stream,) = _STREAM_ID.unpack_from(msg.data)
                if (queue := self._host_in.get(stream)) is not None:
                    queue.put_nowait(msg.data[_STREAM_ID.size :])
                # other binary streams (turn speech) are not used while HA TTS speaks
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

    @callback
    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        ref = msg.get("ref")
        if ref is not None and (fut := self._pending.get(str(ref))) is not None:
            if not fut.done():
                fut.set_result(msg)
            return
        if kind == "stream.end" and (queue := self._host_in.pop(msg["stream"], None)):
            queue.put_nowait(None)
            return
        if kind == "graphs":
            self.graphs = {
                str(g["id"]): str(g.get("name") or g["id"]) for g in msg.get("graphs") or []
            }
            self.default_graph = msg.get("default")
            self.graphs_known = True
            async_dispatcher_send(self.hass, self.graphs_signal)
            return
        if kind == "references":
            if self.on_references is not None:
                self.on_references(dict(msg.get("graphs") or {}))
            return
        if kind == "host.call":
            audio = (msg.get("args") or {}).get("audio")
            if audio is not None:  # register before the first audio frame can arrive
                self._host_in[int(audio["stream"])] = asyncio.Queue()
            task = self.hass.async_create_task(self._serve_host(msg))
            self._host_tasks.add(task)
            task.add_done_callback(self._host_tasks.discard)
            return
        turn_id = msg.get("turn_id")
        handle = self._handles.get(turn_id) if isinstance(turn_id, str) else None
        if handle is None:
            if kind == "error":
                _LOGGER.warning("FlowPilot error: %s", msg.get("message"))
            return
        handle.events.put_nowait(msg)
        if kind == "turn.end":
            self._handles.pop(handle.turn_id, None)

    async def _serve_host(self, msg: dict[str, Any]) -> None:
        call, op = msg.get("call"), str(msg.get("op"))
        handler = self.host_ops.get(op)
        args: dict[str, Any] = msg.get("args") or {}
        audio_stream = args.get("audio", {}).get("stream")
        request = HostRequest(
            self._handles.get(str(msg.get("turn_id"))),
            args,
            audio=self._audio_in(int(audio_stream)) if audio_stream is not None else None,
            send_audio=lambda fmt, chunks: self._audio_out(call, fmt, chunks),
        )
        try:
            if handler is None:
                raise FlowPilotError(f"operation {op!r} is not supported")
            result = await handler(request)
            reply: dict[str, Any] = {
                "type": "host.result",
                "call": call,
                "ok": True,
                "result": result,
            }
        except Exception as err:  # noqa: BLE001 - every failure is reported back to the runtime
            _LOGGER.warning("FlowPilot host operation %s failed: %s", op, err)
            reply = {"type": "host.result", "call": call, "ok": False, "error": str(err)}
        finally:
            if audio_stream is not None:
                self._host_in.pop(int(audio_stream), None)
        try:
            await self.send(reply)
        except FlowPilotError:
            _LOGGER.debug("Could not deliver result of %s: disconnected", op)

    async def _audio_in(self, stream: int) -> AsyncIterator[bytes]:
        queue = self._host_in.get(stream)
        if queue is None:
            return
        while (pcm := await queue.get()) is not None:
            yield pcm

    async def _audio_out(
        self, call: Any, fmt: Mapping[str, Any], chunks: AsyncIterable[bytes]
    ) -> None:
        stream = next(self._streams)
        await self.send({"type": "stream.start", "call": call, "stream": stream, "format": fmt})
        try:
            async for pcm in chunks:
                await self.send_bytes(_STREAM_ID.pack(stream) + pcm)
        finally:
            await self.send({"type": "stream.end", "stream": stream})
