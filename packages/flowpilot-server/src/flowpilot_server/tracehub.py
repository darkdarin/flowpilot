"""Live trace stream for the editor's debug mode.

Every deployment of a graph emits into the hub; editor sockets subscribe per graph. Events carry no
payload content (see ``flowpilot_core.runtime.trace``). A short backlog per graph lets a freshly
opened editor show the last turns.

Editor socket (``GET /api/editor/ws``), JSON text frames:

- client → ``{"type": "hello", "token": "..."}`` then ``{"type": "subscribe", "graph": "main"}``
  (one graph at a time; subscribing again switches)
- server → ``{"type": "hello.ok"}``, ``{"type": "subscribed", "graph": ..}``, then trace events
  ``{"type": "trace", "graph", "turn_id", "event", "node", "t_ms", "attrs"}`` where ``t_ms`` is
  milliseconds since the turn started; ``{"type": "deployed", "graph"}`` after a redeploy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from flowpilot_core.runtime import TraceEvent, TraceSink

if TYPE_CHECKING:
    from .server import FlowPilotServer

_LOGGER = logging.getLogger(__name__)

EDITOR_WS_PATH = "/api/editor/ws"
BACKLOG = 400
QUEUE_SIZE = 1000


class _GraphSink:
    def __init__(self, hub: TraceHub, graph_id: str) -> None:
        self._hub, self._graph_id = hub, graph_id

    def emit(self, event: TraceEvent) -> None:
        self._hub.publish(self._graph_id, event)


class TraceHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._backlog: dict[str, deque[dict[str, Any]]] = {}
        self._turn_start: dict[str, int] = {}

    def sink(self, graph_id: str) -> TraceSink:
        return _GraphSink(self, graph_id)

    def publish(self, graph_id: str, event: TraceEvent) -> None:
        if event.event == "turn.start":
            self._turn_start[event.turn_id] = event.t_mono_ns
        start = self._turn_start.get(event.turn_id, event.t_mono_ns)
        if event.event == "turn.end":
            self._turn_start.pop(event.turn_id, None)
        self._send(
            graph_id,
            {
                "type": "trace",
                "graph": graph_id,
                "turn_id": event.turn_id,
                "event": event.event,
                "node": event.node_id,
                "t_ms": round((event.t_mono_ns - start) / 1e6, 3),
                "attrs": _jsonable(event.attrs),
            },
            keep=True,
        )

    def announce(self, graph_id: str, kind: str) -> None:
        self._send(graph_id, {"type": kind, "graph": graph_id}, keep=False)

    def _send(self, graph_id: str, msg: dict[str, Any], *, keep: bool) -> None:
        if keep:
            self._backlog.setdefault(graph_id, deque(maxlen=BACKLOG)).append(msg)
        for queue in self._subscribers.get(graph_id, ()):
            with contextlib.suppress(asyncio.QueueFull):  # a slow editor loses events, not us
                queue.put_nowait(msg)

    def subscribe(self, graph_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        for msg in self._backlog.get(graph_id, ()):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(msg)
        self._subscribers.setdefault(graph_id, set()).add(queue)

    def unsubscribe(self, graph_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.get(graph_id, set()).discard(queue)


def _jsonable(attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v if isinstance(v, str | int | float | bool) or v is None else str(v)
        for k, v in attrs.items()
    }


def _auth_error(message: str) -> dict[str, str]:
    return {"type": "error", "code": "auth", "message": message}


async def editor_socket(request: web.Request, server: FlowPilotServer) -> web.WebSocketResponse:
    hub = server.trace_hub
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    server.track_socket(ws)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_SIZE)
    graph: str | None = None

    async def pump() -> None:
        while True:
            await ws.send_json(await queue.get())

    writer = asyncio.create_task(pump())
    authed = False
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = msg.json()
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "hello":
                authed = server.authorized(request, data.get("token"))
                if not authed:
                    await ws.send_json(
                        {"type": "error", "code": "auth", "message": "invalid token"}
                    )
                    break
                await ws.send_json({"type": "hello.ok"})
            elif not authed:
                await ws.send_json(_auth_error("hello first"))
                break
            elif kind == "subscribe":
                if graph is not None:
                    hub.unsubscribe(graph, queue)
                graph = str(data.get("graph"))
                await ws.send_json({"type": "subscribed", "graph": graph})
                hub.subscribe(graph, queue)
    finally:
        if graph is not None:
            hub.unsubscribe(graph, queue)
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionError):
            await writer
        server.untrack_socket(ws)
    return ws
