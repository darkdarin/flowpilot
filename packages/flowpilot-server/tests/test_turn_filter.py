"""Voice or text turn: typed turns get their answer without text-to-speech."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import Node
from flowpilot_server.answer import FixedAnswer
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.server import FlowPilotServer

AUTH = {"Authorization": "Bearer t"}
ANSWER = " ".join(f"word{i}" for i in range(100))  # more pieces than a channel buffers

GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "main",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "dev.fixed_stt", "config": {"text": "hi"}},
        {"id": "first", "type": "text.first"},
        {
            "id": "agent",
            "type": "debug.answer",
            "config": {"text": ANSWER, "mode": "words", "word_delay_ms": 0},
        },
        {"id": "turn", "type": "logic.turn_filter"},
        {"id": "tts", "type": "dev.beep_tts"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "stt.utterance", "to": "first.primary"},
        {"from": "in.utterance", "to": "first.fallback"},
        {"from": "first.value", "to": "agent.utterance"},
        {"from": "agent.response", "to": "out.response"},
        {"from": "agent.delta", "to": "turn.text"},
        {"from": "turn.spoken", "to": "tts.text"},
        {"from": "turn.typed", "to": "out.delta"},
        {"from": "tts.speech", "to": "out.speech"},
    ],
}


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[tuple[aiohttp.ClientSession, Any]]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, FixedAnswer, *DEV_NODES]
    for cls in classes:
        registry.register(cls)
    store = GraphStore(tmp_path)
    store.save("main", GRAPH)
    manager = GraphManager(registry, store=store)
    assert await manager.load_all() == {}
    server = TestServer(FlowPilotServer(token="t", default_graph="main", manager=manager).app())
    await server.start_server()
    async with aiohttp.ClientSession(headers=AUTH) as http:
        yield http, server.make_url
    await server.close()
    await manager.stop()


def wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(6400))
    return out.getvalue()


async def test_typed_turn_gets_no_speech(runtime: Any) -> None:
    http, url = runtime
    result = await (await http.post(url(TURN_PATH), json={"text": "hi"})).json()
    assert result["status"] == "ok", result
    assert result["response"]["text"] == ANSWER
    assert result["speech"] is None  # text-to-speech never ran
    assert "tts" not in result["nodes"] or result["nodes"]["tts"] == "ok"


async def test_spoken_turn_is_spoken(runtime: Any) -> None:
    http, url = runtime
    resp = await http.post(url(TURN_PATH), data=wav(), headers={"Content-Type": "audio/wav"})
    result = await resp.json()
    assert result["status"] == "ok", result
    assert result["speech"]["bytes"] == 2 * 160 * len(ANSWER)
