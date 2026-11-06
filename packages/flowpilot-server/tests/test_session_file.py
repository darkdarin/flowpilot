"""Dialogues survive a restart and a redeploy."""

from __future__ import annotations

import stat
import time
from pathlib import Path

from flowpilot_core.runtime import MemorySessionStore
from flowpilot_server.server import SessionIndex
from flowpilot_server.session_file import SessionFile

TELEGRAM = {"ha.conversation_id": "telegram-100"}
SATELLITE = {"ha.conversation_id": "01JC8Z3V4M6X0R2T5Y7W9B1D3F"}


async def test_dialogues_come_back_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    state, index = MemorySessionStore(), SessionIndex()
    file = SessionFile(path, state, index)
    session = index.resolve(TELEGRAM)
    await state.view(session.session_id, "llm.agent").set("history:a", [{"role": "user"}], 60)
    await state.view(session.session_id, "llm.agent").set("gone", 1, ttl_s=-1)
    await state.view(session.session_id, "x").set("obj", object())
    assert file.save() is True
    assert file.save() is False  # nothing changed since
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    state2, index2 = MemorySessionStore(), SessionIndex()
    SessionFile(path, state2, index2).load()
    again = index2.resolve(TELEGRAM)
    assert again.session_id == session.session_id and again.turn_index == 1
    view = state2.view(session.session_id, "llm.agent")
    assert await view.get("history:a") == [{"role": "user"}]
    assert await view.get("gone") is None  # expired entries are not saved
    assert state2.dump() == state.dump()  # the value that is not JSON stayed behind


async def test_a_damaged_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{not json")
    state, index = MemorySessionStore(), SessionIndex()
    SessionFile(path, state, index).load()
    assert state.dump() == [] and index.dump() == []


def test_named_dialogues_outlive_home_assistant_ones() -> None:
    index = SessionIndex(idle_s=60, named_idle_s=3600)
    telegram, satellite = index.resolve(TELEGRAM), index.resolve(SATELLITE)
    silent = [dict(r, last_used=time.time() - 600) for r in index.dump()]  # ten quiet minutes
    restored = SessionIndex(idle_s=60, named_idle_s=3600)
    restored.load(silent)
    assert restored.resolve(TELEGRAM).session_id == telegram.session_id
    assert restored.resolve(SATELLITE).session_id != satellite.session_id


async def test_a_redeploy_keeps_the_dialogues(tmp_path: Path) -> None:
    from flowpilot_server.__main__ import build_registry
    from flowpilot_server.manager import GraphManager, GraphStore

    graph = {
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
    manager = GraphManager(build_registry(dev=True), store=GraphStore(tmp_path))
    first = await manager.deploy_data("main", graph)
    await first.sessions.view("s", "llm.agent").set("history:a", [1])
    second = await manager.deploy_data("main", graph)
    assert await second.sessions.view("s", "llm.agent").get("history:a") == [1]
    await manager.stop()
