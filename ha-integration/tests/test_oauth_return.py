"""An MCP server's OAuth answer comes back through Home Assistant to the runtime that started the
sign-in (ADR-025): Ingress, where the editor runs, would refuse a cross-site return (its cookie is
SameSite=Strict)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestServer
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flowpilot.oauth import register
from flowpilot_core import NodeRegistry
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.server import FlowPilotServer

TOKEN = "t"


async def runtime(
    waiting: str, got: list[tuple[str, str | None, str | None]], data: Path
) -> TestServer:
    manager = GraphManager(NodeRegistry(), store=GraphStore(data))

    def callback(state: str, code: str | None, error: str | None) -> bool:
        got.append((state, code, error))
        return state == waiting

    manager.mcp.callback = callback  # type: ignore[method-assign]
    server = TestServer(FlowPilotServer(token=TOKEN, default_graph="main", manager=manager).app())
    await server.start_server()
    return server


async def test_the_answer_reaches_the_runtime_that_waits(
    hass: HomeAssistant, hass_client_no_auth: Any, tmp_path: Path
) -> None:
    assert await async_setup_component(hass, "http", {})
    got: list[tuple[str, str | None, str | None]] = []
    other = await runtime("someone-else", [], tmp_path / "a")
    mine = await runtime("s1", got, tmp_path / "b")
    try:
        for server in (other, mine):
            entry = MockConfigEntry(
                domain="flowpilot",
                data={"url": str(server.make_url("")).rstrip("/"), "token": TOKEN},
            )
            entry.add_to_hass(hass)
            entry.mock_state(hass, ConfigEntryState.LOADED)
        register(hass)
        register(hass)  # once per Home Assistant
        client = await hass_client_no_auth()  # the browser, not signed in to Home Assistant
        page = await client.get("/api/flowpilot/oauth/callback?code=c1&state=s1")
        assert page.status == 200 and "Вход выполнен" in await page.text()
        assert got == [("s1", "c1", None)]
        stale = await client.get("/api/flowpilot/oauth/callback?code=c2&state=old")
        assert "устарел" in await stale.text()
        assert (await client.get("/api/flowpilot/oauth/callback?code=x")).status == 400
    finally:
        await other.close()
        await mine.close()
