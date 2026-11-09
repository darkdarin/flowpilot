"""End to end: Home Assistant's quick commands in front of an agent, answers joined by First
present.

A command Home Assistant understands is done by it (no agent); anything else reaches the agent.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.conversation import async_converse
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_mock_service

from .conftest import _runtime, setup_flowpilot

pytestmark = pytest.mark.usefixtures("socket_enabled")
AGENT_ENTITY = "conversation.flowpilot_agent"

GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "test",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "quick", "type": "ha.quick"},
        {
            "id": "agent",
            "type": "debug.answer",
            "config": {"text": "echo: {text}", "mode": "words"},
        },
        {"id": "answer", "type": "text.first"},
        {"id": "speech", "type": "text.first"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.utterance", "to": "quick.utterance"},
        {"from": "quick.unhandled", "to": "agent.utterance"},
        {"from": "quick.response", "to": "answer.primary"},
        {"from": "agent.response", "to": "answer.fallback"},
        {"from": "answer.value", "to": "out.response"},
        {"from": "quick.delta", "to": "speech.primary"},
        {"from": "agent.delta", "to": "speech.fallback"},
        {"from": "speech.value", "to": "out.delta"},
    ],
}


async def test_quick_commands_skip_the_agent(hass: HomeAssistant) -> None:
    for domain in ("intent", "light"):
        assert await async_setup_component(hass, domain, {})
    async for runtime in _runtime({}, GRAPH):
        await setup_flowpilot(hass, runtime)
        hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
        async_expose_entity(hass, "conversation", "light.kitchen", True)
        calls = async_mock_service(hass, "light", "turn_on")

        done = await async_converse(
            hass, "turn on kitchen light", None, Context(), agent_id=AGENT_ENTITY
        )
        speech = done.response.speech["plain"]["speech"]
        assert calls and not speech.startswith("echo:")  # Home Assistant answered, not the agent
        other = await async_converse(
            hass, "what rhymes with orange", done.conversation_id, Context(), agent_id=AGENT_ENTITY
        )
        assert other.response.speech["plain"]["speech"] == "echo: what rhymes with orange"
        quick = runtime.trace.named("ha.quick")
        assert [e.attrs["handled"] for e in quick] == [True, False]


async def test_a_room_in_another_case_is_understood(hass: HomeAssistant) -> None:
    """Russian: Home Assistant knows the area "Гостиная", people say "в гостиной"."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    hass.config.language = "ru"
    for domain in ("intent", "light"):
        assert await async_setup_component(hass, domain, {})
    async for runtime in _runtime({}, GRAPH):
        await setup_flowpilot(hass, runtime)
        living = ar.async_get(hass).async_create("Гостиная")
        entry = er.async_get(hass).async_get_or_create("light", "test", "ceiling")
        er.async_get(hass).async_update_entity(entry.entity_id, area_id=living.id)
        hass.states.async_set(entry.entity_id, "off", {"friendly_name": "Люстра"})
        async_expose_entity(hass, "conversation", entry.entity_id, True)
        calls = async_mock_service(hass, "light", "turn_on")

        plain = await async_converse(
            hass,
            "включи свет в гостиной",
            None,
            Context(),
            agent_id="conversation.home_assistant",
            language="ru",
        )
        assert plain.response.response_type.value == "error"  # Home Assistant alone misses it

        done = await async_converse(
            hass, "включи свет в гостиной", None, Context(), agent_id=AGENT_ENTITY, language="ru"
        )
        assert calls, done.response.speech
        [event] = runtime.trace.named("ha.quick")
        assert event.attrs["handled"] is True and event.attrs["retried"] is True


async def test_a_learned_command_does_several_things(hass: HomeAssistant) -> None:
    """A command taught earlier: dim the ceiling and turn on the floor lamp, without an LLM."""
    from flowpilot_sdk import Action
    from flowpilot_server.commands_store import CommandStore

    hass.config.language = "en"
    for domain in ("llm", "intent", "light"):
        assert await async_setup_component(hass, domain, {})
    commands = CommandStore(None)
    commands.add(
        ["movie night"],
        [
            Action("intent__HassTurnOff", {"name": "Ceiling light"}),
            Action("intent__HassTurnOn", {"name": "Floor lamp"}),
        ],
        "Enjoy the film",
        created_by=None,
        source="llm.agent",
    )
    async for runtime in _runtime({}, GRAPH, commands=commands):
        await setup_flowpilot(hass, runtime)
        for entity, name in (("light.ceiling", "Ceiling light"), ("light.floor", "Floor lamp")):
            hass.states.async_set(entity, "on", {"friendly_name": name})
            async_expose_entity(hass, "conversation", entity, True)
        off = async_mock_service(hass, "light", "turn_off")
        on = async_mock_service(hass, "light", "turn_on")
        user = await hass.auth.async_create_user("Pavel")

        result = await async_converse(
            hass, "Movie night, please!", None, Context(user_id=user.id), agent_id=AGENT_ENTITY
        )
        assert result.response.speech["plain"]["speech"] == "Enjoy the film"
        assert [c.data["entity_id"] for c in off] == [["light.ceiling"]]
        assert [c.data["entity_id"] for c in on] == [["light.floor"]]
        assert on[0].context.user_id == user.id  # as the person who asked
        assert commands.all()[0].uses == 1


async def test_a_command_confirmed_with_a_sound_says_what_it_did(hass: HomeAssistant) -> None:
    """Quick commands' ack wired to the client output: the answer carries acknowledge (on/off)
    in its speech's extra_data, for satellites with their own sound (a Yandex Station)."""
    graph = {
        **GRAPH,
        "nodes": [
            {**n, "config": {"acknowledge": "always"}} if n["id"] == "quick" else n
            for n in GRAPH["nodes"]
        ],
        "edges": [*GRAPH["edges"], {"from": "quick.ack", "to": "out.ack"}],
    }
    for domain in ("intent", "light"):
        assert await async_setup_component(hass, domain, {})
    async for runtime in _runtime({}, graph):
        await setup_flowpilot(hass, runtime)
        hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
        async_expose_entity(hass, "conversation", "light.kitchen", True)
        async_mock_service(hass, "light", "turn_on")
        async_mock_service(hass, "light", "turn_off")

        on = await async_converse(
            hass, "turn on kitchen light", None, Context(), agent_id=AGENT_ENTITY
        )
        assert on.response.speech["plain"]["extra_data"] == {"acknowledge": "on"}
        off = await async_converse(
            hass, "turn off kitchen light", None, Context(), agent_id=AGENT_ENTITY
        )
        assert off.response.speech["plain"]["extra_data"] == {"acknowledge": "off"}
        chat = await async_converse(
            hass, "what rhymes with orange", None, Context(), agent_id=AGENT_ENTITY
        )
        assert not chat.response.speech["plain"].get("extra_data")  # an answer in words
