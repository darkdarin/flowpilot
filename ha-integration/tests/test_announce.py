"""The agent says a text on the speakers the person named (from a chat, too)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.flowpilot.announce import TOOL_NAME
from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.host import build_host_ops


async def setup(hass: HomeAssistant) -> tuple[Any, list[Any]]:
    for domain in ("homeassistant", "llm", "conversation", "intent"):
        assert await async_setup_component(hass, domain, {})
    registry = er.async_get(hass)
    satellites = [
        ("kitchen", "Станция", "Кухня", AssistSatelliteEntityFeature.ANNOUNCE),
        ("bedroom", "Мини", "Спальня", AssistSatelliteEntityFeature.ANNOUNCE),
        ("hall", "Старая", "Прихожая", 0),  # cannot announce
    ]
    for key, name, area, features in satellites:
        entry = registry.async_get_or_create(
            "assist_satellite", "test", key, supported_features=features
        )
        area_id = ar.async_get(hass).async_get_or_create(area).id
        registry.async_update_entity(entry.entity_id, area_id=area_id)
        hass.states.async_set(entry.entity_id, "idle", {"friendly_name": name})
    calls = async_mock_service(hass, "assist_satellite", "announce")
    return build_host_ops(hass, MockConfigEntry(domain="flowpilot")), calls


def say(**arguments: Any) -> HostRequest:
    return HostRequest(None, {"name": TOOL_NAME, "arguments": arguments, "language": "ru"})


async def test_the_tool_lists_the_speakers_that_announce(hass: HomeAssistant) -> None:
    host, _ = await setup(hass)
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "ru"}))
    tool = next(t for t in result["tools"] if t["name"] == TOOL_NAME)
    assert tool["parameters"]["properties"]["speakers"]["items"]["enum"] == [
        "Мини (Спальня)",
        "Станция (Кухня)",
    ]


async def test_a_text_is_said_on_the_named_speakers(hass: HomeAssistant) -> None:
    host, calls = await setup(hass)
    user = await hass.auth.async_create_user("Pavel")
    request = say(message="Обед готов", speakers=["кухня"])
    done = await host["ha.llm.call"](HostRequest(None, {**request.args, "user_id": user.id}))
    assert done == {"error": False, "data": {"announcing_on": ["Станция (Кухня)"]}}
    await hass.async_block_till_done()
    [call] = calls
    assert call.data["message"] == "Обед готов"
    assert call.data["entity_id"] == ["assist_satellite.test_kitchen"]
    assert call.context.user_id == user.id

    done = await host["ha.llm.call"](say(message="Спать", speakers=["Мини (Спальня)", "Станция"]))
    assert done["error"] is False, done
    await hass.async_block_till_done()
    assert set(calls[1].data["entity_id"]) == {
        "assist_satellite.test_bedroom",
        "assist_satellite.test_kitchen",
    }


async def test_an_unknown_or_missing_speaker_is_an_error(hass: HomeAssistant) -> None:
    host, calls = await setup(hass)
    missing = await host["ha.llm.call"](say(message="Привет"))
    assert missing["error"] is True and "Станция (Кухня)" in missing["data"]["message"]
    unknown = await host["ha.llm.call"](say(message="Привет", speakers=["гараж"]))
    assert unknown["error"] is True
    silent = await host["ha.llm.call"](say(message="Привет", speakers=["Старая (Прихожая)"]))
    assert silent["error"] is True
    await hass.async_block_till_done()
    assert not calls


async def test_the_only_speaker_needs_no_name(hass: HomeAssistant) -> None:
    for domain in ("homeassistant", "llm", "conversation", "intent"):
        assert await async_setup_component(hass, domain, {})
    entry = er.async_get(hass).async_get_or_create(
        "assist_satellite",
        "test",
        "only",
        supported_features=AssistSatelliteEntityFeature.ANNOUNCE,
    )
    hass.states.async_set(entry.entity_id, "idle", {"friendly_name": "Станция"})
    calls = async_mock_service(hass, "assist_satellite", "announce")
    host = build_host_ops(hass, MockConfigEntry(domain="flowpilot"))
    done = await host["ha.llm.call"](say(message="Обед готов"))
    assert done == {"error": False, "data": {"announcing_on": ["Станция"]}}
    await hass.async_block_till_done()
    assert calls[0].data["entity_id"] == [entry.entity_id]
