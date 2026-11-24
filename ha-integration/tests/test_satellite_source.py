"""A voice turn names its satellite from the start, so tools need not wait for recognition.

Home Assistant tells the conversation agent which device heard a command only after speech is
recognized; an agent that hears speech itself (``llm.voice``) asks for Home Assistant's tools while
the person still speaks. We rely on public state: an assist satellite turns ``listening`` on its
pipeline's STT start, just before the pipeline calls speech-to-text (``AssistSatelliteEntity``,
Home Assistant 2026.11). The tests set that state and check what FlowPilot does with it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.host import build_host_ops
from custom_components.flowpilot.stt import listening_source

from .conftest import Runtime, setup_flowpilot
from .test_integration import run_voice_turn

pytestmark = pytest.mark.usefixtures("socket_enabled")


def station(hass: HomeAssistant, entity_id: str = "assist_satellite.station") -> str:
    """A satellite entity on a device in the living room; returns its device id."""
    config = MockConfigEntry(domain="test")
    config.add_to_hass(hass)
    area = ar.async_get(hass).async_get_or_create("Гостиная")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config.entry_id, identifiers={("test", entity_id)}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    er.async_get(hass).async_get_or_create(
        "assist_satellite",
        "test",
        entity_id,
        suggested_object_id=entity_id.split(".")[1],
        device_id=device.id,
        config_entry=config,
    )
    return device.id


async def test_the_listening_satellite_is_the_source(hass: HomeAssistant) -> None:
    device_id = station(hass)
    hass.states.async_set("assist_satellite.station", "listening")
    hass.states.async_set("assist_satellite.kitchen", "idle")
    assert listening_source(hass) == {
        "satellite_id": "assist_satellite.station",
        "device_id": device_id,
        "area": "Гостиная",
    }


async def test_no_guess_when_none_or_several_listen(hass: HomeAssistant) -> None:
    assert listening_source(hass) == {}
    hass.states.async_set("assist_satellite.station", "listening")
    hass.states.async_set("assist_satellite.kitchen", "listening")
    assert listening_source(hass) == {}


async def test_a_voice_turn_starts_with_its_satellite(
    hass: HomeAssistant, echo_runtime: Runtime
) -> None:
    entry = await setup_flowpilot(hass, echo_runtime)
    device_id = station(hass)
    hass.states.async_set("assist_satellite.station", "listening")
    client = entry.runtime_data
    sources: list[dict[str, Any]] = []
    start = client.start_turn

    async def recording(**kwargs: Any) -> Any:
        sources.append(dict(kwargs["source"]))
        return await start(**kwargs)

    with patch.object(client, "start_turn", recording):
        events: list[PipelineEvent] = await run_voice_turn(hass)
    assert any(e.type == PipelineEventType.STT_END for e in events)
    assert sources == [
        {
            "kind": "ha.assist",
            "satellite_id": "assist_satellite.station",
            "device_id": device_id,
            "area": "Гостиная",
        }
    ]


async def test_tools_do_not_wait_for_recognition_once_the_satellite_is_named(
    hass: HomeAssistant,
) -> None:
    for domain in ("homeassistant", "llm", "conversation", "intent", "light"):
        assert await async_setup_component(hass, domain, {})
    host = build_host_ops(hass, MockConfigEntry(domain="flowpilot"))
    # a voice turn whose Assist request Home Assistant has not bound yet (still listening)
    handle: Any = SimpleNamespace(attached=asyncio.get_running_loop().create_future())
    named = {"language": "ru", "device_id": "dev1", "satellite_id": "assist_satellite.station"}
    result = await asyncio.wait_for(host["ha.llm.tools"](HostRequest(handle, named)), 1.0)
    assert result["tools"]
    with pytest.raises(TimeoutError):  # unnamed: it waits for recognition, as before
        await asyncio.wait_for(host["ha.llm.tools"](HostRequest(handle, {"language": "ru"})), 0.3)
