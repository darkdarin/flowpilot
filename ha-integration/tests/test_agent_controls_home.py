"""End to end: an Assist chat → a FlowPilot graph with the LLM agent → Home Assistant's tools.

The model is a fake OpenAI-compatible API that asks to turn the kitchen light on; everything else
is real: Home Assistant, the FlowPilot integration, the runtime and the agent node.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.components.conversation import async_converse
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from .conftest import Runtime, _runtime, graph, setup_flowpilot

pytestmark = pytest.mark.usefixtures("socket_enabled")
AGENT_ENTITY = "conversation.flowpilot_agent"


class FakeModel:
    """Asks for HassTurnOn on the first request, then answers with what the tool returned."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(delta: dict[str, Any]) -> None:
            chunk = {"choices": [{"delta": delta, "index": 0}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

        if body["messages"][-1]["role"] == "user":
            names = [t["function"]["name"] for t in body.get("tools", [])]
            tool = next(n for n in names if n.endswith("HassTurnOn"))
            args = json.dumps({"name": "Kitchen light"})
            call = {"index": 0, "id": "c0", "function": {"name": tool, "arguments": args}}
            await send({"tool_calls": [call]})
        else:
            await send({"content": "Turned on."})
        await resp.write(b"data: [DONE]\n\n")
        return resp


@pytest.fixture
async def model() -> AsyncIterator[tuple[FakeModel, str]]:
    fake = FakeModel()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("/v1"))
    await server.close()


async def test_the_agent_turns_on_a_light_as_the_user(
    hass: HomeAssistant, model: tuple[FakeModel, str]
) -> None:
    fake, url = model
    agent = {"type": "llm.agent", "config": {"base_url": url, "model": "m"}}
    async for runtime in _runtime({}, graph(agent), extra=("llm.agent",)):
        await check(hass, fake, runtime)


async def check(hass: HomeAssistant, fake: FakeModel, runtime: Runtime) -> None:
    for domain in ("llm", "intent", "light"):
        assert await async_setup_component(hass, domain, {})
    await setup_flowpilot(hass, runtime)
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    async_expose_entity(hass, "conversation", "light.kitchen", True)
    calls = async_mock_service(hass, "light", "turn_on")
    user = await hass.auth.async_create_user("Pavel")

    result = await async_converse(
        hass, "turn on the kitchen light", None, Context(user_id=user.id), agent_id=AGENT_ENTITY
    )

    assert result.response.speech["plain"]["speech"] == "Turned on."
    [call] = calls
    assert call.data["entity_id"] == ["light.kitchen"]
    assert call.context.user_id == user.id  # Home Assistant ran it as the person who asked
    first, second = fake.requests
    assert any("HassTurnOn" in t["function"]["name"] for t in first["tools"])
    assert second["messages"][-1]["role"] == "tool"
