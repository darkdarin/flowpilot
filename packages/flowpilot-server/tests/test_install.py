"""Container installs: data directory, the Ingress port, Supervisor discovery."""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_server import datadir, supervisor
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource, HaConversation
from flowpilot_server.server import FlowPilotServer

TOKEN = "install-token"


def test_data_dir_is_created_once(tmp_path: Path) -> None:
    config_path = datadir.init_data_dir(tmp_path / "data", addon=False)
    data = config_path.parent
    config = json.loads(config_path.read_text())
    assert config["token_file"] == "token" and "ingress" not in config
    token = (data / "token").read_text().strip()
    assert len(token) >= 40
    assert stat.S_IMODE((data / "token").stat().st_mode) == 0o600
    assert json.loads((data / "graphs" / "main.json").read_text())["id"] == "main"

    (data / "graphs" / "main.json").unlink()
    (data / "graphs" / "mine.json").write_text("{}")
    datadir.init_data_dir(data, addon=True)
    assert (data / "token").read_text().strip() == token  # never regenerated
    assert "ingress" not in json.loads(config_path.read_text())  # an existing config is kept
    assert not (data / "graphs" / "main.json").exists()  # no starter next to the user's graphs


def test_addon_config_opens_the_ingress_port(tmp_path: Path) -> None:
    config = json.loads(datadir.init_data_dir(tmp_path, addon=True).read_text())
    assert config["ingress"] == {"port": 8099, "trusted_peers": ["172.30.32.2"]}
    (tmp_path / "options.json").write_text('{"log_level": "debug", "dev_nodes": true}')
    assert datadir.addon_options(tmp_path) == {"log_level": "debug", "dev_nodes": True}


def test_starter_graph_is_valid(tmp_path: Path) -> None:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, HaConversation):
        registry.register(cls)
    manager = GraphManager(registry, store=GraphStore(tmp_path))
    server = FlowPilotServer(token=TOKEN, default_graph="main", manager=manager)
    assert server.manager.issues(datadir.STARTER_GRAPH) == []


@pytest.fixture
async def apps(tmp_path: Path) -> AsyncIterator[tuple[aiohttp.ClientSession, Any, Any]]:
    """The same runtime on a normal port and on an Ingress port that trusts 127.0.0.1."""
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, HaConversation):
        registry.register(cls)
    store = GraphStore(tmp_path)
    store.save("main", datadir.STARTER_GRAPH)
    manager = GraphManager(registry, store=store)
    server = FlowPilotServer(token=TOKEN, default_graph="main", manager=manager)
    await manager.load_all()
    plain = TestServer(server.app())
    ingress = TestServer(server.app(trusted_peers=["127.0.0.1"]))
    await plain.start_server()
    await ingress.start_server()
    async with aiohttp.ClientSession() as session:
        yield session, plain.make_url, ingress.make_url
    await plain.close()
    await ingress.close()
    await manager.stop()


async def test_ingress_port_trusts_the_supervisor(apps: Any) -> None:
    session, plain, ingress = apps
    assert (await session.get(plain("/api/editor/catalog"))).status == 401
    assert (await session.get(ingress("/api/editor/catalog"))).status == 200
    async with session.ws_connect(ingress("/api/editor/ws")) as ws:
        await ws.send_json({"type": "hello"})
        assert (await ws.receive_json())["type"] == "hello.ok"
    async with session.ws_connect(ingress("/api/ws")) as ws:
        await ws.send_json({"type": "hello", "protocol": 1, "client": {"name": "x"}})
        assert (await ws.receive_json())["type"] == "hello.ok"


async def test_ingress_paths_keep_the_prefix(apps: Any) -> None:
    session, plain, ingress = apps
    prefix = {"X-Ingress-Path": "/api/hassio_ingress/abc"}
    resp = await session.get(ingress("/"), headers=prefix, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/api/hassio_ingress/abc/editor/"
    resp = await session.get(ingress("/editor"), headers=prefix, allow_redirects=False)
    assert resp.headers["Location"] == "/api/hassio_ingress/abc/editor/"
    # the header means nothing from an untrusted peer
    resp = await session.get(plain("/editor"), headers=prefix, allow_redirects=False)
    assert resp.headers["Location"] == "/editor/"
    resp = await session.get(plain("/"), headers=prefix, allow_redirects=False)
    assert resp.status == 200  # the dev client


async def test_discovery_announces_host_port_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, Any]] = []

    async def discovery(request: web.Request) -> web.Response:
        received.append((request.headers["Authorization"], await request.json()))
        return web.json_response({"result": "ok", "data": {"uuid": "1"}})

    app = web.Application()
    app.router.add_post("/discovery", discovery)
    fake = TestServer(app)
    await fake.start_server()
    try:
        assert await supervisor.announce(8765, TOKEN, url=str(fake.make_url(""))) is False
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sv-token")
        url = str(fake.make_url("")).rstrip("/")
        assert await supervisor.announce(8765, TOKEN, url=url) is True
    finally:
        await fake.close()
    [(auth, body)] = received
    assert auth == "Bearer sv-token"
    assert body["service"] == "flowpilot"
    assert body["config"]["port"] == 8765 and body["config"]["token"] == TOKEN
    assert body["config"]["host"]


async def test_discovery_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sv-token")
    assert await supervisor.announce(8765, TOKEN, url="http://127.0.0.1:9") is False
