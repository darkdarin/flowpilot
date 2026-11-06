"""Runtime secrets: the store file, the editor API and redeploys when a secret changes."""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import NodeContext, NodeSpec, PortSpec, Response, TurnSession
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.secret_store import SecretStore
from flowpilot_server.server import FlowPilotServer

TOKEN = "secret-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Keyed:
    """Answers with the secret it was started with (only in tests)."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="test.keyed",
        version="1.0.0",
        title="Keyed",
        category="agent",
        inputs={"utterance": PortSpec("text.utterance/1", kind="value")},
        outputs={"response": PortSpec("agent.response/1", kind="value")},
        config_schema={
            "type": "object",
            "properties": {"api_key": {"type": "secret"}},
            "required": ["api_key"],
        },
        turn_timeout_s=5.0,
    )
    started: ClassVar[list[str]] = []

    def __init__(self) -> None:
        self._key = ""

    async def start(self, ctx: NodeContext) -> None:
        self._key = ctx.secret(ctx.config["api_key"]["secret"])
        Keyed.started.append(self._key)

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        if await turn.input("utterance").value() is not None:
            await turn.output("response").send(Response(text=self._key))


GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "main",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "agent", "type": "test.keyed", "config": {"api_key": {"secret": "soniox"}}},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.utterance", "to": "agent.utterance"},
        {"from": "agent.response", "to": "out.response"},
    ],
}


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[Any, Any, GraphManager]]:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, Keyed):
        registry.register(cls)
    store = GraphStore(tmp_path / "graphs")
    store.save("main", GRAPH)
    secrets = SecretStore(tmp_path / "secrets.json")
    manager = GraphManager(registry, store=store, secrets=secrets)
    Keyed.started = []
    failed = await manager.load_all()
    assert "missing secrets" in failed["main"]  # no key yet: the graph is not running
    server = FlowPilotServer(token=TOKEN, default_graph="main", manager=manager)
    ts = TestServer(server.app())
    await ts.start_server()
    async with aiohttp.ClientSession(headers=AUTH) as session:
        yield session, ts.make_url, manager
    await ts.close()
    await manager.stop()


async def test_set_use_and_remove_a_secret(api: Any, tmp_path: Path) -> None:
    session, url, manager = api
    listing = await (await session.get(url("/api/editor/secrets"))).json()
    assert listing == {"secrets": [{"name": "soniox", "set": False, "graphs": ["main"]}]}
    detail = await (await session.get(url("/api/editor/graphs/main"))).json()
    assert [w["code"] for w in detail["warnings"]] == ["missing_secret"]

    resp = await session.put(url("/api/editor/secrets/soniox"), json={"value": "key-1"})
    assert resp.status == 200 and (await resp.json())["redeployed"] == {"main": None}
    assert Keyed.started == ["key-1"] and "main" in manager.deployments
    path = tmp_path / "secrets.json"
    assert json.loads(path.read_text()) == {"soniox": "key-1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    listing = await (await session.get(url("/api/editor/secrets"))).json()
    assert listing["secrets"] == [{"name": "soniox", "set": True, "graphs": ["main"]}]
    assert "key-1" not in json.dumps(listing)  # values never come back

    await session.put(url("/api/editor/secrets/soniox"), json={"value": "key-2"})
    assert Keyed.started[-1] == "key-2"  # the node restarted with the new value
    assert SecretStore(path)["soniox"] == "key-2"  # and it survives a restart

    resp = await session.delete(url("/api/editor/secrets/soniox"))
    assert "missing secrets" in (await resp.json())["redeployed"]["main"]
    assert "main" in manager.deployments  # the running version stays


async def test_rejects_bad_names_and_values(api: Any) -> None:
    session, url, _ = api
    resp = await session.put(url("/api/editor/secrets/bad%20name"), json={"value": "x"})
    assert resp.status == 400
    resp = await session.put(url("/api/editor/secrets/ok"), json={"value": ""})
    assert resp.status == 400
    resp = await session.put(url("/api/editor/secrets/ok"), json={"nope": 1})
    assert resp.status == 400


async def test_needs_the_token(api: Any) -> None:
    _, url, _ = api
    async with aiohttp.ClientSession() as anonymous:
        assert (await anonymous.get(url("/api/editor/secrets"))).status == 401
