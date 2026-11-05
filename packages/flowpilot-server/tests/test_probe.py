"""The debug recording node and the editor's recordings API, through a real runtime."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from yarl import URL

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import Node
from flowpilot_server.answer import FixedAnswer
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.probe import DebugProbe, recordings_dir
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.server import FlowPilotServer

TOKEN = "rec-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RATE = 16000


def graph(**record: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "voice",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "stt", "type": "dev.fixed_stt", "config": {"text": "hello"}},
            {"id": "agent", "type": "debug.answer", "config": {"text": "echo: {text}"}},
            {"id": "out", "type": "client.sink"},
            {"id": "rec", "type": "debug.probe", "config": record},
        ],
        "edges": [
            {"from": "in.audio", "to": "stt.audio"},
            {"from": "in.audio", "to": "rec.audio"},
            {"from": "stt.utterance", "to": "agent.utterance"},
            {"from": "agent.response", "to": "out.response"},
        ],
    }


def wav(seconds: float) -> tuple[bytes, bytes]:
    pcm = bytes(range(256)) * (int(seconds * RATE * 2) // 256)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue(), pcm


async def runtime(
    tmp_path: Path, *, watch: bool = True, **record: Any
) -> AsyncIterator[tuple[Any, Any, Path]]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, DebugProbe, FixedAnswer, *DEV_NODES]
    for cls in classes:
        registry.register(cls)
    store = GraphStore(tmp_path / "graphs")
    store.save("voice", graph(**record))
    manager = GraphManager(registry, store=store, data_dir=tmp_path)
    server = FlowPilotServer(token=TOKEN, default_graph="voice", manager=manager)
    assert await manager.load_all() == {}
    ts = TestServer(server.app())
    await ts.start_server()
    async with aiohttp.ClientSession() as session:
        # the recorder is a debug tap: it records only while live debug watches the graph
        async with session.ws_connect(ts.make_url("/api/editor/ws")) as ws:
            await ws.send_json({"type": "hello", "token": TOKEN})
            await ws.receive_json()
            await ws.send_json({"type": "subscribe", "graph": "voice", "debug": watch})
            await ws.receive_json()
            yield session, ts.make_url, recordings_dir(tmp_path)
    await ts.close()
    await manager.stop()


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[tuple[Any, Any, Path]]:
    async for value in runtime(tmp_path, keep=2):
        yield value


async def turn(session: Any, url: Any, body: bytes) -> Any:
    resp = await session.post(
        url(TURN_PATH), data=body, headers={**AUTH, "Content-Type": "audio/wav"}
    )
    assert resp.status == 200
    return await resp.json()


async def test_a_turn_is_recorded_and_served(rt: Any) -> None:
    session, url, directory = rt
    body, pcm = wav(0.5)
    result = await turn(session, url, body)
    assert result["status"] == "ok" and result["response"]["text"] == "echo: hello"

    listing = await (await session.get(url("/api/editor/recordings"), headers=AUTH)).json()
    [rec] = listing["recordings"]
    assert (rec["graph"], rec["node"], rec["turn_id"]) == ("voice", "rec", result["turn_id"])
    assert rec["seconds"] == 0.5

    resp = await session.get(url(f"/api/editor/recordings/{rec['file']}"), headers=AUTH)
    assert resp.headers["Content-Type"] == "audio/wav"
    with wave.open(io.BytesIO(await resp.read())) as w:
        assert (w.getframerate(), w.getnchannels()) == (RATE, 1)
        assert w.readframes(w.getnframes()) == pcm  # what the client sent, unchanged

    other = await session.get(url("/api/editor/recordings"), params={"graph": "x"}, headers=AUTH)
    assert (await other.json())["recordings"] == []
    assert (await session.get(url("/api/editor/recordings"))).status == 401


async def test_only_the_newest_are_kept_and_files_can_be_deleted(rt: Any) -> None:
    session, url, directory = rt
    for _ in range(3):
        await turn(session, url, wav(0.1)[0])
    files = sorted(p.name for p in directory.glob("*.wav"))
    assert len(files) == 2  # keep=2
    resp = await session.delete(url(f"/api/editor/recordings/{files[0]}"), headers=AUTH)
    assert resp.status == 200 and not (directory / files[0]).exists()
    base = str(url("/api/editor/recordings/"))
    for bad in ("..%2Fgraphs%2Fvoice.json", "notes.txt", files[0]):  # traversal, foreign, deleted
        resp = await session.get(URL(base + bad, encoded=True), headers=AUTH)
        assert resp.status == 404


async def test_long_audio_is_cut(tmp_path: Path) -> None:
    async for session, url, _ in runtime(tmp_path, max_seconds=0.2):
        await turn(session, url, wav(1.0)[0])
        listing = await (await session.get(url("/api/editor/recordings"), headers=AUTH)).json()
        assert listing["recordings"][0]["seconds"] == 0.2


async def test_nothing_is_recorded_without_live_debug(tmp_path: Path) -> None:
    async for session, url, directory in runtime(tmp_path, watch=False):
        result = await turn(session, url, wav(0.3)[0])
        assert result["status"] == "ok"  # the turn does not wait for the idle recorder
        assert "rec" not in result["nodes"]  # it did not even run
        assert not directory.exists() or not any(directory.glob("*.wav"))
