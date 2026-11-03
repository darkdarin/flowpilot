"""Fixtures: a real FlowPilot runtime in the test event loop and the integration using it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import custom_components
import pytest
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.runtime import Deployment, ListTraceSink
from flowpilot_sdk import Node
from flowpilot_server.devnodes import DEV_NODES
from flowpilot_server.nodes import ClientSink, ClientSource, HaConversation, HaStt, HaTts
from flowpilot_server.server import FlowPilotServer

# The test harness puts its own ``custom_components`` package first on sys.path; add ours to it.
_OURS = str(Path(__file__).resolve().parent.parent / "custom_components")
if _OURS not in custom_components.__path__:
    custom_components.__path__.insert(0, _OURS)

TOKEN = "test-token"
PHRASE = "what time is it"


def graph(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "test",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "stt", "type": "dev.fixed_stt", "config": {"text": PHRASE, "max_chunks": 10}},
            {"id": "utt", "type": "text.first"},
            {"id": "agent", **agent},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "in.audio", "to": "stt.audio"},
            {"from": "stt.transcript", "to": "out.transcript"},
            {"from": "stt.utterance", "to": "utt.primary"},
            {"from": "in.utterance", "to": "utt.fallback"},
            {"from": "utt.value", "to": "agent.utterance"},
            {"from": "agent.delta", "to": "out.delta"},
            {"from": "agent.response", "to": "out.response"},
        ],
    }


SPEECH_GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "test",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "ha.stt", "config": {"entity_id": "stt.fake"}},
        {"id": "utt", "type": "text.first"},
        {"id": "agent", "type": "dev.echo_agent"},
        {"id": "tts", "type": "ha.tts", "config": {"engine": "tts.fake"}},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "stt.transcript", "to": "out.transcript"},
        {"from": "stt.utterance", "to": "utt.primary"},
        {"from": "in.utterance", "to": "utt.fallback"},
        {"from": "utt.value", "to": "agent.utterance"},
        {"from": "agent.delta", "to": "out.delta"},
        {"from": "agent.delta", "to": "tts.text"},
        {"from": "tts.speech", "to": "out.speech"},
        {"from": "agent.response", "to": "out.response"},
    ],
}

ECHO_AGENT = {"type": "dev.echo_agent"}
HA_AGENT = {"type": "ha.conversation", "config": {"agent_id": "conversation.home_assistant"}}


@dataclass
class Runtime:
    url: str
    trace: ListTraceSink

    def turns(self) -> int:
        return len(self.trace.named("turn.start"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Iterator[None]:
    yield


async def _runtime(
    agent: dict[str, Any], graph_data: dict[str, Any] | None = None
) -> AsyncIterator[Runtime]:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [
        ClientSource, ClientSink, HaConversation, HaStt, HaTts, *DEV_NODES
    ]  # fmt: skip
    for cls in classes:
        registry.register(cls)
    trace = ListTraceSink()
    deployment = Deployment(
        validate_graph(parse_graph(graph_data or graph(agent)), registry, grants=GrantPolicy()),
        registry,
        trace=trace,
    )
    await deployment.start()
    server = TestServer(
        FlowPilotServer({"test": deployment}, token=TOKEN, default_graph="test").app()
    )
    await server.start_server()
    try:
        yield Runtime(str(server.make_url("")).rstrip("/"), trace)
    finally:
        await server.close()
        await deployment.stop()


@pytest.fixture
async def echo_runtime() -> AsyncIterator[Runtime]:
    async for rt in _runtime(ECHO_AGENT):
        yield rt


@pytest.fixture
async def ha_agent_runtime() -> AsyncIterator[Runtime]:
    async for rt in _runtime(HA_AGENT):
        yield rt


async def setup_flowpilot(hass: HomeAssistant, runtime: Runtime) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "assist_pipeline", {})
    entry = MockConfigEntry(domain="flowpilot", data={"url": runtime.url, "token": TOKEN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = entry.runtime_data
    for _ in range(100):
        if client.connected:
            break
        await asyncio.sleep(0.02)
    assert client.connected
    return entry


@pytest.fixture
async def speech_runtime() -> AsyncIterator[Runtime]:
    async for rt in _runtime({}, SPEECH_GRAPH):
        yield rt
