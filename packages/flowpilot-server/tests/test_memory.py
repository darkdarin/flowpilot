"""The long-term memory: the store and the editor's API."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_server.__main__ import build_registry
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.memory_store import MemoryStore
from flowpilot_server.server import FlowPilotServer

GRAPH = {
    "schema_version": 1,
    "id": "main",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "answer", "type": "debug.answer", "config": {"text": "hi"}},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.utterance", "to": "answer.utterance"},
        {"from": "answer.response", "to": "out.response"},
    ],
}


def test_facts_are_kept_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    memory = MemoryStore(path)
    home = memory.add("  The cat is   called Barsik. ", None, source="editor")
    own = memory.add("Masha does not eat meat.", "masha", source="llm.agent")
    assert home.text == "The cat is called Barsik."
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = MemoryStore(path)
    assert again.all() == [home, own]
    assert again.facts(None) == [home]
    assert again.facts("masha") == [home, own]
    assert again.facts("pavel") == [home]
    with pytest.raises(ValueError):
        again.add("   ", None, source="editor")
    assert again.remove(own.id) and not again.remove(own.id)
    assert MemoryStore(path).all() == [home]


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[aiohttp.ClientSession, Any]]:
    store = GraphStore(tmp_path)
    store.save("main", GRAPH)
    manager = GraphManager(
        build_registry(dev=True), store=store, memory=MemoryStore(tmp_path / "m.json")
    )
    assert await manager.load_all() == {}
    server = TestServer(FlowPilotServer(token="t", default_graph="main", manager=manager).app())
    await server.start_server()
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer t"}) as http:
        yield http, server.make_url
    await server.close()
    await manager.stop()


async def test_the_editor_manages_facts(api: Any) -> None:
    http, url = api
    added = await (
        await http.post(url("/api/editor/memory"), json={"text": "Cat: Barsik", "owner": None})
    ).json()
    assert added["owner"] is None and added["source"] == "editor"
    resp = await http.put(
        url(f"/api/editor/memory/{added['id']}"), json={"text": "Cat: Murzik", "owner": "masha"}
    )
    assert (await resp.json())["text"] == "Cat: Murzik"
    listed = await (await http.get(url("/api/editor/memory"))).json()
    assert [(f["text"], f["owner"]) for f in listed["facts"]] == [("Cat: Murzik", "masha")]
    assert listed["people"] == [] and listed["people_known"] is False  # no Home Assistant here
    bad = await http.post(url("/api/editor/memory"), json={"text": ""})
    assert bad.status == 400
    assert (await http.delete(url(f"/api/editor/memory/{added['id']}"))).status == 200
    assert (await http.delete(url(f"/api/editor/memory/{added['id']}"))).status == 404
