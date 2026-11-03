"""Graph store, live redeploy and the editor REST API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import NodeContext, NodeSpec, PortSpec, Response, TurnSession
from flowpilot_server.manager import GraphManager, GraphStore, issue_dicts
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.server import FlowPilotServer

TOKEN = "editor-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Reply:
    """Answers with a configured text after an optional gate opens."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="test.reply",
        version="1.0.0",
        title="Reply",
        category="agent",
        inputs={"utterance": PortSpec("text.utterance/1", kind="value")},
        outputs={"response": PortSpec("agent.response/1", kind="value")},
        config_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        turn_timeout_s=5.0,
    )
    gate: ClassVar[asyncio.Event | None] = None
    entered: ClassVar[asyncio.Event | None] = None

    async def start(self, ctx: NodeContext) -> None:
        self.text = str(ctx.config["text"])
        if self.text == "boom":
            raise RuntimeError("cannot start")

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        if await turn.input("utterance").value() is None:
            return
        if Reply.entered is not None:
            Reply.entered.set()
        if Reply.gate is not None:
            await Reply.gate.wait()
        await turn.output("response").send(Response(text=self.text))


def graph(text: str, graph_id: str = "main") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": graph_id,
        "name": "Main",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "agent", "type": "test.reply", "config": {"text": text}},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "in.utterance", "to": "agent.utterance"},
            {"from": "agent.response", "to": "out.response"},
        ],
    }


def registry() -> NodeRegistry:
    reg = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, Reply):
        reg.register(cls)
    return reg


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[tuple[aiohttp.ClientSession, Any, GraphManager]]:
    store = GraphStore(tmp_path)
    store.save("main", graph("v1"))
    manager = GraphManager(registry(), store=store)
    server = FlowPilotServer(token=TOKEN, default_graph="main", manager=manager)
    assert await manager.load_all() == {}
    ts = TestServer(server.app())
    await ts.start_server()
    async with aiohttp.ClientSession() as session:
        yield session, ts.make_url, manager
    await ts.close()
    await manager.stop()
    Reply.gate = Reply.entered = None


async def ask(session: aiohttp.ClientSession, url: Any) -> Any:
    resp = await session.post(url(TURN_PATH), json={"text": "hi"}, headers=AUTH)
    assert resp.status == 200
    return (await resp.json())["response"]["text"]


def test_store_keeps_backup_and_ignores_drafts(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.save("a", {"v": 1})
    store.save("a", {"v": 2})
    store.save("a", {"v": 3}, draft=True)
    assert store.ids() == ["a"]
    assert store.load("a") == {"v": 2}
    assert store.load("a", draft=True) == {"v": 3}
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}
    store.delete_draft("a")
    assert store.load("a", draft=True) is None
    with pytest.raises(ValueError):
        store.load("../etc")


def test_issue_dicts_point_at_nodes_and_edges() -> None:
    manager = GraphManager(registry())
    data = graph("x")
    data["nodes"][1]["config"] = {}
    data["edges"].append({"from": "agent.response", "to": "out.nope"})
    issues = manager.issues(data)
    assert any(i.get("node") == "agent" for i in issues), issues
    assert any(i.get("edge") == 2 for i in issues), issues
    assert issue_dicts([], data) == []


async def test_editor_requires_token(env: Any) -> None:
    session, url, _ = env
    resp = await session.get(url("/api/editor/catalog"))
    assert resp.status == 401


async def test_catalog_describes_nodes_and_types(env: Any) -> None:
    session, url, _ = env
    body = await (await session.get(url("/api/editor/catalog"), headers=AUTH)).json()
    reply = next(n for n in body["nodes"] if n["type"] == "test.reply")
    assert reply["inputs"] == [
        {
            "name": "utterance",
            "type": "text.utterance/1",
            "kind": "value",
            "required": True,
            "delivery": "lossless",
        }
    ]
    assert reply["config_schema"]["required"] == ["text"]
    types = {t["id"]: t["privileged"] for t in body["types"]}
    assert types["identity.principal/1"] is True
    assert types["text.utterance/1"] is False


async def test_draft_validate_and_deploy(env: Any) -> None:
    session, url, manager = env
    assert await ask(session, url) == "v1"

    listing = await (await session.get(url("/api/editor/graphs"), headers=AUTH)).json()
    assert listing["graphs"] == [
        {"id": "main", "name": "Main", "deployed": True, "has_draft": False, "default": True}
    ]

    bad = graph("v2")
    bad["edges"].pop()
    resp = await session.put(url("/api/editor/graphs/main/draft"), json=bad, headers=AUTH)
    assert (await resp.json())["issues"], "an unwired sink must be reported"
    resp = await session.post(url("/api/editor/graphs/main/deploy"), headers=AUTH)
    assert resp.status == 422
    assert (await resp.json())["issues"]
    assert await ask(session, url) == "v1"  # the running graph is untouched

    resp = await session.put(url("/api/editor/graphs/main/draft"), json=graph("v2"), headers=AUTH)
    assert (await resp.json())["issues"] == []
    detail = await (await session.get(url("/api/editor/graphs/main"), headers=AUTH)).json()
    assert detail["deployed"]["nodes"][1]["config"]["text"] == "v1"
    assert detail["draft"]["nodes"][1]["config"]["text"] == "v2"

    resp = await session.post(url("/api/editor/graphs/main/deploy"), headers=AUTH)
    assert resp.status == 200
    assert await ask(session, url) == "v2"
    detail = await (await session.get(url("/api/editor/graphs/main"), headers=AUTH)).json()
    assert detail["draft"] is None and detail["deployed"]["nodes"][1]["config"]["text"] == "v2"
    assert manager.store.load("main")["nodes"][1]["config"]["text"] == "v2"

    resp = await session.post(url("/api/editor/graphs/main/deploy"), headers=AUTH)
    assert resp.status == 409  # nothing left to deploy


async def test_failed_start_keeps_running_graph(env: Any) -> None:
    session, url, _ = env
    await session.put(url("/api/editor/graphs/main/draft"), json=graph("boom"), headers=AUTH)
    resp = await session.post(url("/api/editor/graphs/main/deploy"), headers=AUTH)
    assert resp.status == 500
    assert "cannot start" in (await resp.json())["error"]
    assert await ask(session, url) == "v1"


async def test_in_flight_turn_finishes_on_old_deployment(env: Any) -> None:
    session, url, manager = env
    old = manager.deployments["main"]
    Reply.gate, Reply.entered = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(ask(session, url))
    await asyncio.wait_for(Reply.entered.wait(), 5)

    await manager.deploy_data("main", graph("v2"))
    assert manager.deployments["main"] is not old
    Reply.gate.set()
    assert await asyncio.wait_for(pending, 5) == "v1"  # pinned to the deployment it started on
    assert await ask(session, url) == "v2"


async def test_create_and_discard_graph(env: Any) -> None:
    session, url, _ = env
    resp = await session.post(url("/api/editor/graphs"), json={"id": "Bad Id"}, headers=AUTH)
    assert resp.status == 400
    resp = await session.post(
        url("/api/editor/graphs"), json={"id": "kids", "name": "Kids"}, headers=AUTH
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["issues"], "a fresh graph has an unwired sink"
    resp = await session.post(url("/api/editor/graphs"), json={"id": "kids"}, headers=AUTH)
    assert resp.status == 409

    listing = await (await session.get(url("/api/editor/graphs"), headers=AUTH)).json()
    kids = next(g for g in listing["graphs"] if g["id"] == "kids")
    assert kids == {"id": "kids", "name": "Kids", "deployed": False, "has_draft": True,
                    "default": False}  # fmt: skip

    resp = await session.delete(url("/api/editor/graphs/kids/draft"), headers=AUTH)
    assert resp.status == 200
    resp = await session.get(url("/api/editor/graphs/kids"), headers=AUTH)
    assert resp.status == 404


async def test_trace_stream_for_live_debug(env: Any) -> None:
    session, url, manager = env
    async with session.ws_connect(url("/api/editor/ws")) as ws:
        await ws.send_json({"type": "subscribe", "graph": "main"})
        assert (await ws.receive_json())["code"] == "auth"
    async with session.ws_connect(url("/api/editor/ws")) as ws:
        await ws.send_json({"type": "hello", "token": TOKEN})
        assert (await ws.receive_json())["type"] == "hello.ok"
        await ws.send_json({"type": "subscribe", "graph": "main"})
        assert await ws.receive_json() == {"type": "subscribed", "graph": "main"}

        assert await ask(session, url) == "v1"
        events: list[dict[str, Any]] = []
        while not events or events[-1]["event"] != "turn.end":
            events.append(await asyncio.wait_for(ws.receive_json(), 5))
        kinds = {(e["event"], e["node"]) for e in events}
        assert {("turn.start", None), ("node.start", "agent"), ("node.end", "agent")} <= kinds
        port = next(e for e in events if e["event"] == "port.end" and e["node"] == "agent")
        assert port["attrs"] == {"port": "response", "items": 1, "state": "end"}
        absent = next(e for e in events if e["event"] == "port.end" and e["node"] == "in"
                      and e["attrs"]["port"] == "audio")  # fmt: skip
        assert absent["attrs"]["state"] == "absent"
        assert all(e["t_ms"] >= 0 for e in events)
        assert events[-1]["attrs"]["status"] == "ok"

        await manager.deploy_data("main", graph("v2"))
        assert await asyncio.wait_for(ws.receive_json(), 5) == {"type": "deployed", "graph": "main"}

    # a new editor sees the recent turns from the backlog
    async with session.ws_connect(url("/api/editor/ws")) as ws:
        await ws.send_json({"type": "hello", "token": TOKEN})
        await ws.receive_json()
        await ws.send_json({"type": "subscribe", "graph": "main"})
        await ws.receive_json()
        first = await asyncio.wait_for(ws.receive_json(), 5)
        assert first["event"] == "turn.start"


def test_editor_files_stay_inside_the_build(tmp_path: Path) -> None:
    from flowpilot_server.server import editor_file

    assert editor_file("index.html", tmp_path) is None  # not built
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>")
    (tmp_path / "assets" / "app.js").write_text("x")
    (tmp_path.parent / "secret.txt").write_text("s")
    assert editor_file("assets/app.js", tmp_path) == (tmp_path / "assets" / "app.js").resolve()
    assert editor_file("graphs/main", tmp_path) == (tmp_path / "index.html").resolve()
    assert editor_file("../secret.txt", tmp_path) == (tmp_path / "index.html").resolve()


async def test_editor_root_redirects(env: Any) -> None:
    session, url, _ = env
    resp = await session.get(url("/editor"), allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/editor/"
