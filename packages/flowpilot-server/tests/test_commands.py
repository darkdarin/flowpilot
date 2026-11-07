"""Learned quick commands: the store and the editor's API."""

from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_sdk import Action
from flowpilot_server.__main__ import build_registry
from flowpilot_server.commands_store import CommandStore
from flowpilot_server.manager import GraphManager, GraphStore
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

MOVIE = [
    Action("intent__HassTurnOff", {"area": "Гостиная"}),
    Action("intent__HassTurnOn", {"name": "ТВ"}),
]


def test_commands_are_kept_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "quick_commands.json"
    store = CommandStore(path)
    movie = store.add(
        [" режим  кино ", "Режим кино", "кино"], MOVIE, "Приятного просмотра",
        created_by="pavel", source="llm.agent",
    )  # fmt: skip
    assert movie.phrases == ("режим кино", "кино")  # cleaned, no repeats
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.used(movie.id)
    again = CommandStore(path)
    [kept] = again.all()
    assert kept.actions == movie.actions and kept.uses == 1 and kept.created_by == "pavel"
    with pytest.raises(ValueError):
        again.add([], MOVIE, "", created_by=None, source="editor")
    with pytest.raises(ValueError):
        again.add(["x"], [Action("save_quick_command", {})], "", created_by=None, source="editor")
    assert again.remove(movie.id) and not again.remove(movie.id)
    assert CommandStore(path).all() == []


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[aiohttp.ClientSession, Any, CommandStore]]:
    store = GraphStore(tmp_path)
    store.save("main", GRAPH)
    commands = CommandStore(tmp_path / "c.json")
    manager = GraphManager(build_registry(dev=True), store=store, commands=commands)
    assert await manager.load_all() == {}
    server = TestServer(FlowPilotServer(token="t", default_graph="main", manager=manager).app())
    await server.start_server()
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer t"}) as http:
        yield http, server.make_url, commands
    await server.close()
    await manager.stop()


async def test_the_editor_manages_commands(api: Any) -> None:
    http, url, commands = api
    movie = commands.add(["режим кино"], MOVIE, "", created_by=None, source="llm.agent")
    listed = await (await http.get(url("/api/editor/commands"))).json()
    assert [c["phrases"] for c in listed["commands"]] == [["режим кино"]]
    assert listed["commands"][0]["actions"][1] == {
        "tool": "intent__HassTurnOn",
        "arguments": {"name": "ТВ"},
    }
    changed = await http.put(
        url(f"/api/editor/commands/{movie.id}"), json={"phrases": ["кино", "фильм"], "reply": "Ок"}
    )
    assert changed.status == 200 and commands.all()[0].phrases == ("кино", "фильм")
    bad = await http.put(url(f"/api/editor/commands/{movie.id}"), json={"phrases": []})
    assert bad.status == 400
    assert (await http.delete(url(f"/api/editor/commands/{movie.id}"))).status == 200
    assert (await http.delete(url(f"/api/editor/commands/{movie.id}"))).status == 404
