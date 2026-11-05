"""The Fixed text debugging node, through a real runtime: at once, word by word, after an input."""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import Node
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.fixed_text import FixedText
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.server import FlowPilotServer

TOKEN = "text-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def graph(after: bool = False, **config: Any) -> dict[str, Any]:
    edges = [
        {"from": "say.response", "to": "out.response"},
        {"from": "say.delta", "to": "out.delta"},
    ]
    if after:  # stand in for an agent: answer after the speech-to-text result
        edges += [
            {"from": "in.audio", "to": "stt.audio"},
            {"from": "stt.utterance", "to": "say.after"},
        ]
    nodes = [
        {"id": "in", "type": "client.source"},
        {"id": "say", "type": "debug.text", "config": config},
        {"id": "out", "type": "client.sink"},
    ]
    if after:
        nodes.append({"id": "stt", "type": "dev.fixed_stt", "config": {"text": "hi"}})
    return {"schema_version": 1, "id": "main", "nodes": nodes, "edges": edges}


async def runtime(tmp_path: Path, data: dict[str, Any]) -> AsyncIterator[tuple[Any, Any]]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, FixedText, *DEV_NODES]
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


async def text_turn(session: Any, url: Any) -> tuple[Any, int]:
    """One text turn; returns the HTTP result and how many deltas the node sent."""
    async with session.ws_connect(url("/api/editor/ws")) as ws:
        await ws.send_json({"type": "hello", "token": TOKEN})
        await ws.receive_json()
        await ws.send_json({"type": "subscribe", "graph": "main"})
        await ws.receive_json()
        resp = await session.post(url(TURN_PATH), json={"text": "x"}, headers=AUTH)
        result = await resp.json()
        deltas = -1
        while True:
            event = await asyncio.wait_for(ws.receive_json(), 5)
            if event.get("event") == "port.end" and event["node"] == "say":
                if event["attrs"]["port"] == "delta":
                    deltas = event["attrs"]["items"]
            if event.get("event") == "turn.end":
                return result, deltas


async def test_at_once(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph(text="Сейчас три часа")):
        result, deltas = await text_turn(session, url)
        assert result["status"] == "ok"
        assert result["response"]["text"] == "Сейчас три часа"
        assert deltas == 1


async def test_word_by_word(tmp_path: Path) -> None:
    data = graph(text="one two three four", mode="words", word_delay_ms=50)
    async for session, url in runtime(tmp_path, data):
        started = time.monotonic()
        result, deltas = await text_turn(session, url)
        assert result["response"]["text"] == "one two three four"
        assert deltas == 4
        assert time.monotonic() - started >= 0.15  # three pauses between four words


async def test_after_waits_for_its_input(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph(after=True, text="answer")):
        # a text turn has no audio, so no speech-to-text result: nothing to answer
        result, deltas = await text_turn(session, url)
        assert result["response"] is None and deltas == 0


async def test_after_answers_a_voice_turn(tmp_path: Path) -> None:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(6400))
    async for session, url in runtime(tmp_path, graph(after=True, text="answer")):
        resp = await session.post(
            url(TURN_PATH), data=out.getvalue(), headers={**AUTH, "Content-Type": "audio/wav"}
        )
        result = await resp.json()
        assert result["status"] == "ok" and result["response"]["text"] == "answer"


async def test_play_starts_an_empty_turn(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph(text="hello", when="play")):
        # a real turn: the node waits for its Play button and stays silent
        result, deltas = await text_turn(session, url)
        assert result["response"] is None and deltas == 0
        # the Play button: a turn with no input, started by this node
        resp = await session.post(url(f"{TURN_PATH}?trigger=say"), json={}, headers=AUTH)
        result = await resp.json()
        assert result["status"] == "ok" and result["response"]["text"] == "hello"
        # another node's Play button does not count
        resp = await session.post(url(f"{TURN_PATH}?trigger=other"), json={}, headers=AUTH)
        assert (await resp.json())["response"] is None


async def test_play_does_not_wait_for_after(tmp_path: Path) -> None:
    async for session, url in runtime(tmp_path, graph(after=True, text="answer")):
        resp = await session.post(url(f"{TURN_PATH}?trigger=say"), json={}, headers=AUTH)
        result = await resp.json()
        assert result["status"] == "ok" and result["response"]["text"] == "answer"
