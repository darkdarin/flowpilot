"""One FlowPilot speech-to-text and agent entity per deployed graph."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from homeassistant.components.assist_pipeline import PipelineEventType
from homeassistant.components.conversation import async_converse
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.runtime import Deployment
from flowpilot_sdk import Node
from flowpilot_server.answer import FixedAnswer
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.manager import BUILTIN_GRANTS
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.server import FlowPilotServer

from .conftest import PHRASE, TOKEN, Runtime, graph, setup_flowpilot
from .test_integration import event, run_voice_turn

pytestmark = [
    pytest.mark.usefixtures("socket_enabled"),
    pytest.mark.parametrize("expected_lingering_timers", [True]),  # the runtime's websockets
]


def echo(graph_id: str, name: str, prefix: str) -> dict[str, Any]:
    data = graph({"type": "debug.answer", "config": {"text": f"{prefix}{{text}}"}})
    return {**data, "id": graph_id, "name": name}


@pytest.fixture
async def two_graphs() -> AsyncIterator[Runtime]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, FixedAnswer, *DEV_NODES]
    for cls in classes:
        registry.register(cls)
    deployments = {}
    for data in (echo("main", "Main", "main: "), echo("kids", "Kids", "kids: ")):
        vg = validate_graph(parse_graph(data), registry, grants=GrantPolicy(BUILTIN_GRANTS))
        deployments[data["id"]] = Deployment(vg, registry)
        await deployments[data["id"]].start()
    server = TestServer(FlowPilotServer(deployments, token=TOKEN, default_graph="main").app())
    await server.start_server()
    try:
        yield Runtime(str(server.make_url("")).rstrip("/"), None)  # type: ignore[arg-type]
    finally:
        await server.close()
        for d in deployments.values():
            await d.stop()


async def ask(hass: HomeAssistant, agent: str) -> str:
    result = await async_converse(hass, "hi", None, Context(), agent_id=agent)
    return str(result.response.speech["plain"]["speech"])


async def test_each_graph_gets_its_entities(hass: HomeAssistant, two_graphs: Runtime) -> None:
    entry = await setup_flowpilot(hass, two_graphs)
    await hass.async_block_till_done()
    assert hass.states.get("stt.flowpilot_speech_to_text") is not None  # the default graph
    assert hass.states.get("stt.flowpilot_kids") is not None
    assert hass.states.get("stt.flowpilot_main") is not None
    kids = hass.states.get("conversation.flowpilot_kids")
    assert kids is not None and kids.attributes["friendly_name"] == "FlowPilot Kids"

    assert await ask(hass, "conversation.flowpilot_agent") == "main: hi"  # default graph
    assert await ask(hass, "conversation.flowpilot_kids") == "kids: hi"
    assert await ask(hass, "conversation.flowpilot_main") == "main: hi"

    # the runtime renames one graph and no longer has the other
    client = entry.runtime_data
    client._dispatch(
        {"type": "graphs", "graphs": [{"id": "kids", "name": "Children"}], "default": "kids"}
    )
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get("conversation.flowpilot_main") is None
    assert registry.async_get("stt.flowpilot_main") is None
    renamed = hass.states.get("conversation.flowpilot_kids")
    assert renamed is not None and renamed.attributes["friendly_name"] == "FlowPilot Children"
    assert hass.states.get("conversation.flowpilot_agent") is not None  # the default stays


async def test_voice_turn_runs_the_graph_of_its_speech_to_text(
    hass: HomeAssistant, two_graphs: Runtime
) -> None:
    await setup_flowpilot(hass, two_graphs)
    await hass.async_block_till_done()
    kids_stt, kids_agent = "stt.flowpilot_kids", "conversation.flowpilot_kids"

    events = await run_voice_turn(hass, kids_stt, kids_agent)
    speech = event(events, PipelineEventType.INTENT_END)["intent_output"]["response"]["speech"]
    assert speech["plain"]["speech"] == f"kids: {PHRASE}"

    # mixed up in one assistant: the turn still runs the speech-to-text entity's graph
    events = await run_voice_turn(hass, kids_stt, "conversation.flowpilot_agent")
    speech = event(events, PipelineEventType.INTENT_END)["intent_output"]["response"]["speech"]
    assert speech["plain"]["speech"] == f"kids: {PHRASE}"
