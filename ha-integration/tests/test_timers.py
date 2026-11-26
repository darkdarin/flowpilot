"""Timers and a station's alarms from a turn without a speaker of its own (a Telegram chat) go to
a named speaker."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import probatio
from homeassistant.components.intent import TimerInfo, async_register_timer_handler
from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_platform

from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.const import CONF_TIMER_DEVICE
from custom_components.flowpilot.host import build_host_ops
from custom_components.flowpilot.timers import is_timer_tool

START = "intent__HassStartTimer"


async def setup(hass: HomeAssistant, speakers: list[tuple[str, str]], **options: Any) -> Any:
    """``speakers``: (name, area) of the devices that ring timers; returns the host operations,
    the device ids by name and the timers each device got."""
    for domain in ("homeassistant", "llm", "conversation", "intent"):
        assert await async_setup_component(hass, domain, {})
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    ids: dict[str, str] = {}
    rang: dict[str, list[TimerInfo]] = {}
    for name, area in speakers:
        area_id = ar.async_get(hass).async_get_or_create(area).id
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=owner.entry_id, identifiers={("test", name)}, name=name
        )
        dr.async_get(hass).async_update_device(device.id, area_id=area_id)
        ids[name] = device.id
        rang[device.id] = []

        def handler(event: Any, timer: TimerInfo, got: list[TimerInfo] = rang[device.id]) -> None:
            got.append(timer)

        async_register_timer_handler(hass, device.id, handler)
    entry = MockConfigEntry(domain="flowpilot", options=options)
    return build_host_ops(hass, entry), ids, rang


def start(**arguments: Any) -> HostRequest:
    return HostRequest(
        None, {"name": START, "arguments": {"minutes": 5, **arguments}, "language": "ru"}
    )


async def test_a_chat_timer_gets_a_speaker_argument(hass: HomeAssistant) -> None:
    host, _, _ = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "ru"}))
    timer = next(t for t in result["tools"] if t["name"].endswith(START))
    assert timer["parameters"]["properties"]["device"]["enum"] == [
        "Мини (Спальня)",
        "Станция (Кухня)",
    ]
    others = [t for t in result["tools"] if not is_timer_tool(t["name"])]
    assert others and all("device" not in t["parameters"].get("properties", {}) for t in others)


async def test_a_voice_timer_stays_on_its_own_speaker(hass: HomeAssistant) -> None:
    host, ids, rang = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    own = {"language": "ru", "device_id": ids["Мини"]}
    result = await host["ha.llm.tools"](HostRequest(None, own))
    timer = next(t for t in result["tools"] if t["name"].endswith(START))
    assert "device" not in timer["parameters"].get("properties", {})
    request = start()
    done = await host["ha.llm.call"](HostRequest(None, {**request.args, "device_id": ids["Мини"]}))
    assert done["error"] is False, done
    assert [len(rang[ids["Мини"]]), len(rang[ids["Станция"]])] == [1, 0]


async def test_a_chat_timer_rings_on_the_named_speaker(hass: HomeAssistant) -> None:
    host, ids, rang = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    done = await host["ha.llm.call"](start(device="кухня"))
    assert done["error"] is False, done
    assert [len(rang[ids["Станция"]]), len(rang[ids["Мини"]])] == [1, 0]
    done = await host["ha.llm.call"](start(device="Мини (Спальня)"))
    assert done["error"] is False, done
    assert len(rang[ids["Мини"]]) == 1


async def test_a_chat_timer_without_a_speaker_asks_which(hass: HomeAssistant) -> None:
    host, _, rang = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    asked = await host["ha.llm.call"](start())
    assert asked["error"] is True and "Станция (Кухня)" in asked["data"]["message"]
    unknown = await host["ha.llm.call"](start(device="гараж"))
    assert unknown["error"] is True and not any(rang.values())


async def test_a_chat_timer_rings_on_the_only_speaker(hass: HomeAssistant) -> None:
    host, ids, rang = await setup(hass, [("Колонка", "Гостиная")])
    done = await host["ha.llm.call"](start())
    assert done["error"] is False, done
    assert len(rang[ids["Колонка"]]) == 1


async def test_a_chat_timer_rings_on_the_chosen_speaker(hass: HomeAssistant) -> None:
    host, ids, rang = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    chosen, _, _ = await setup(hass, [], **{CONF_TIMER_DEVICE: ids["Мини"]})
    done = await chosen["ha.llm.call"](start())
    assert done["error"] is False, done
    assert [len(rang[ids["Мини"]]), len(rang[ids["Станция"]])] == [1, 0]


async def test_a_speaker_is_named_by_its_alias(hass: HomeAssistant) -> None:
    """ "Поставь таймер на Алисе": an alias of the station's player names the station."""
    from homeassistant.helpers import entity_registry as er

    host, ids, rang = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    player = er.async_get(hass).async_get_or_create(
        "media_player", "test", "station_player", device_id=ids["Станция"]
    )
    er.async_get(hass).async_update_entity(player.entity_id, aliases=[er.COMPUTED_NAME, "Алиса"])
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "ru"}))
    timer = next(t for t in result["tools"] if t["name"].endswith(START))
    assert "Алиса" in timer["parameters"]["properties"]["device"]["enum"]
    done = await host["ha.llm.call"](start(device="Алисе"))
    assert done["error"] is False, done
    assert [len(rang[ids["Станция"]]), len(rang[ids["Мини"]])] == [1, 0]


class StationAlarm(llm.Tool):
    """Like a Yandex Station's alarm tool: in a turn from a station it sets that station's alarm;
    in a chat the integration asks for its own ``station`` choice."""

    name = "station__set_alarm"
    integration = "station"
    description = "Set an alarm on the station"

    def __init__(self, own: str | None, stations: list[str], set_on: list[str | None]) -> None:
        self._own, self._stations, self._set_on = own, stations, set_on
        schema: dict[Any, Any] = {probatio.Required("time"): str}
        if own is None and len(stations) > 1:
            schema[probatio.Required("station")] = probatio.In(stations)
        self.parameters = probatio.Schema(schema)

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)
        self._set_on.append(self._own or args.get("station") or self._stations[0])
        return llm.ToolResult(data={"success": True})


async def with_stations(hass: HomeAssistant, stations: list[str]) -> list[str | None]:
    """The alarm tool of the speakers named ``stations``; returns where alarms were set."""
    set_on: list[str | None] = []

    def get_tools(hass: HomeAssistant, llm_context: llm.LLMContext, api_id: str) -> LLMTools:
        device = dr.async_get(hass).async_get(llm_context.device_id or "")
        own = device.name if device and device.name in stations else None
        return LLMTools(tools=[StationAlarm(own, stations, set_on)])

    assert await async_setup_component(hass, "llm", {})
    hass.config.components.add("station")
    mock_platform(hass, "station.llm", Mock(async_get_tools=get_tools))
    return set_on


async def test_a_chat_alarm_is_set_on_the_named_station(hass: HomeAssistant) -> None:
    set_on = await with_stations(hass, ["Станция", "Мини"])
    host, _, _ = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня"), ("Voice", "Холл")])
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "ru"}))
    alarm = next(t for t in result["tools"] if t["name"] == StationAlarm.name)
    properties = alarm["parameters"]["properties"]
    assert "station" not in properties
    assert properties["device"]["enum"] == ["Мини (Спальня)", "Станция (Кухня)"]

    def call(**arguments: Any) -> HostRequest:
        args = {"time": "07:00", **arguments}
        return HostRequest(None, {"name": StationAlarm.name, "arguments": args, "language": "ru"})

    done = await host["ha.llm.call"](call(device="кухня"))
    assert done["error"] is False, done
    asked = await host["ha.llm.call"](call())
    assert asked["error"] is True and "Мини (Спальня)" in asked["data"]["message"]
    other = await host["ha.llm.call"](call(device="холл"))
    assert other["error"] is True
    assert set_on == ["Станция"]


async def test_a_chat_alarm_goes_to_the_chosen_station(hass: HomeAssistant) -> None:
    set_on = await with_stations(hass, ["Станция", "Мини"])
    _, ids, _ = await setup(hass, [("Станция", "Кухня"), ("Мини", "Спальня")])
    host, _, _ = await setup(hass, [], **{CONF_TIMER_DEVICE: ids["Мини"]})
    request = {"name": StationAlarm.name, "arguments": {"time": "07:00"}, "language": "ru"}
    done = await host["ha.llm.call"](HostRequest(None, request))
    assert done["error"] is False, done
    assert set_on == ["Мини"]


async def test_one_station_keeps_its_tool(hass: HomeAssistant) -> None:
    """With one station the integration's chat tool needs no choice: it stays as it is."""
    set_on = await with_stations(hass, ["Станция"])
    host, _, _ = await setup(hass, [("Станция", "Кухня"), ("Voice", "Холл")])
    result = await host["ha.llm.tools"](HostRequest(None, {"language": "ru"}))
    alarm = next(t for t in result["tools"] if t["name"] == StationAlarm.name)
    assert "device" not in alarm["parameters"]["properties"]
    request = {"name": StationAlarm.name, "arguments": {"time": "07:00"}, "language": "ru"}
    done = await host["ha.llm.call"](HostRequest(None, request))
    assert done["error"] is False, done
    assert set_on == ["Станция"]
