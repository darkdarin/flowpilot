"""Home Assistant's Assist LLM API as host operations for FlowPilot's agents."""

from __future__ import annotations

from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.host import build_host_ops


async def ops(hass: HomeAssistant) -> Any:
    for domain in ("homeassistant", "llm", "conversation", "intent", "light"):
        assert await async_setup_component(hass, domain, {})
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    hass.states.async_set("light.secret", "off", {"friendly_name": "Hidden light"})
    async_expose_entity(hass, "conversation", "light.kitchen", True)
    async_expose_entity(hass, "conversation", "light.secret", False)
    return build_host_ops(hass, MockConfigEntry(domain="flowpilot"))


async def test_tools_cover_entities_exposed_to_assist(hass: HomeAssistant) -> None:
    host = await ops(hass)
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "en"}))
    names = {t["name"] for t in result["tools"]}
    assert {"intent__HassTurnOn", "intent__HassTurnOff"} <= names
    turn_on = next(t for t in result["tools"] if t["name"] == "intent__HassTurnOn")
    assert turn_on["parameters"]["type"] == "object"
    assert "anyOf" not in turn_on["parameters"]
    assert isinstance(result["prompt"], str) and result["prompt"]


async def test_a_tool_call_controls_an_exposed_entity(hass: HomeAssistant) -> None:
    host = await ops(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    user = await hass.auth.async_create_user("Pavel")
    args = {
        "name": "intent__HassTurnOn",
        "arguments": {"name": "Kitchen light"},
        "user_id": user.id,
    }
    result = await host["ha.llm.call"](HostRequest(None, args))
    assert result["error"] is False, result
    [call] = calls
    assert call.data["entity_id"] == ["light.kitchen"]
    assert call.context.user_id == user.id  # runs as the person who spoke


async def test_hidden_entities_and_bad_calls_come_back_as_errors(hass: HomeAssistant) -> None:
    host = await ops(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    hidden = {"name": "intent__HassTurnOn", "arguments": {"name": "Hidden light"}}
    result = await host["ha.llm.call"](HostRequest(None, hidden))
    assert result["error"] is True and not calls
    unknown = await host["ha.llm.call"](HostRequest(None, {"name": "Nope", "arguments": {}}))
    assert unknown["error"] is True and "Nope" in unknown["data"]["message"]


async def test_conversation_tells_a_command_it_did_not_understand(hass: HomeAssistant) -> None:
    """The built-in agent's error code lets FlowPilot pass what it did not get to an LLM."""
    host = await ops(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    process = host["ha.conversation.process"]
    done = await process(
        HostRequest(
            None, {"text": "turn on kitchen light", "agent_id": "conversation.home_assistant"}
        )
    )
    assert done["response_type"] == "action_done" and done["error_code"] is None
    assert len(calls) == 1
    missed = await process(
        HostRequest(
            None, {"text": "what rhymes with orange", "agent_id": "conversation.home_assistant"}
        )
    )
    assert missed["response_type"] == "error" and missed["error_code"] == "no_intent_match"


async def test_names_assist_knows(hass: HomeAssistant) -> None:
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import entity_registry as er

    host = await ops(hass)
    ar.async_get(hass).async_create("Гостиная", aliases={"Зал"})
    entry = er.async_get(hass).async_get_or_create("light", "test", "lamp")
    er.async_get(hass).async_update_entity(entry.entity_id, aliases=["Торшер"])
    hass.states.async_set(entry.entity_id, "off", {"friendly_name": "Настольная лампа"})
    async_expose_entity(hass, "conversation", entry.entity_id, True)
    result = await host["ha.names.list"](HostRequest(None, {}))
    names = set(result["names"])
    assert {"Гостиная", "Зал", "Настольная лампа", "Торшер", "Kitchen light"} <= names
    assert "Hidden light" not in names  # not exposed to Assist
