"""The Debug probe's text inputs, through a real runtime: it reports texts only in live debug."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import Node
from flowpilot_server.answer import FixedAnswer
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.probe import MAX_CHARS, DebugProbe
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.server import FlowPilotServer

TOKEN = "tap-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def graph(text: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "main",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "say", "type": "debug.answer", "config": {"text": text, "mode": "words"}},
            {"id": "tap", "type": "debug.probe"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "say.response", "to": "out.response"},
            {"from": "say.delta", "to": "out.delta"},
            {"from": "say.delta", "to": "tap.delta"},
            {"from": "in.utterance", "to": "tap.utterance"},
            {"from": "say.response", "to": "tap.response"},
        ],
    }


async def runtime(tmp_path: Path, data: dict[str, Any]) -> AsyncIterator[tuple[Any, Any]]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, FixedAnswer, DebugProbe, *DEV_NODES]
    for cls in classes:
        registry.register(cls)
    store = GraphStore(tmp_path)
    store.save("main", data)
    manager = GraphManager(registry, store=store)
    server = FlowPilotServer(token=TOKEN, default_graph="main", manager=manager)
    assert await manager.load_all() == {}
    ts = TestServer(server.app())
    await ts.start_server()
    async with aiohttp.ClientSession() as session:
        yield session, ts.make_url
    await ts.close()
    await manager.stop()


async def tapped(session: Any, url: Any, debug: bool) -> tuple[Any, dict[str, Any]]:
    """One text turn; returns the HTTP result and the tap's texts by port."""
    async with session.ws_connect(url("/api/editor/ws")) as ws:
        await ws.send_json({"type": "hello", "token": TOKEN})
        await ws.receive_json()
        await ws.send_json({"type": "subscribe", "graph": "main", "debug": debug})
        await ws.receive_json()
        resp = await session.post(url(TURN_PATH), json={"text": "x"}, headers=AUTH)
        result = await resp.json()
        texts: dict[str, Any] = {}
        while True:
            event = await asyncio.wait_for(ws.receive_json(), 5)
            if event.get("event") == "tap.text":
                texts[event["attrs"]["port"]] = event["attrs"]
            if event.get("event") == "turn.end":
                return result, texts


async def test_shows_texts_in_live_debug(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph("one two three")):
        result, texts = await tapped(session, url, debug=True)
        assert result["status"] == "ok" and result["nodes"]["tap"] == "ok"
        assert texts["delta"]["text"] == "one two three" and texts["delta"]["items"] == 3
        assert texts["utterance"]["text"] == "x"  # what the client typed
        assert texts["response"]["text"] == "one two three"


async def test_idle_without_live_debug(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph("one two three")):
        result, texts = await tapped(session, url, debug=False)
        assert result["status"] == "ok" and result["response"]["text"] == "one two three"
        assert "tap" not in result["nodes"] and texts == {}


async def test_long_text_is_cut(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph("x" * (MAX_CHARS + 50))):
        _, texts = await tapped(session, url, debug=True)
        assert len(texts["response"]["text"]) == MAX_CHARS
