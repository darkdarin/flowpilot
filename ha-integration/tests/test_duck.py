"""Media players of a listening satellite's area get quiet and come back before the command runs.

The players here are states with service handlers that change them, like a television's
integration would; the satellite is a state that goes ``listening`` → ``processing`` → ``idle`` as
``AssistSatelliteEntity`` does (Home Assistant 2026.11).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.components.media_player.const import MediaPlayerEntityFeature as F
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.const import (
    CONF_DUCK,
    CONF_DUCK_EXCLUDE,
    CONF_DUCK_FALLBACK,
    CONF_DUCK_LEVEL,
)
from custom_components.flowpilot.duck import MediaDucker
from custom_components.flowpilot.host import build_host_ops

SATELLITE = "assist_satellite.station"
VOLUME = F.VOLUME_SET | F.VOLUME_MUTE
calls: list[tuple[str, str, dict[str, Any]]] = []


def place(hass: HomeAssistant, entity_id: str, area: str, device: str | None = None) -> str:
    """Register an entity on a device (one per name, of one config entry) in an area; returns
    the device id."""
    configs = hass.config_entries.async_entries("test")
    if configs:
        config_id = configs[0].entry_id
    else:
        config = MockConfigEntry(domain="test")
        config.add_to_hass(hass)
        config_id = config.entry_id
    area_id = ar.async_get(hass).async_get_or_create(area).id
    found = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_id, identifiers={("test", device or entity_id)}
    )
    dr.async_get(hass).async_update_device(found.id, area_id=area_id)
    domain, object_id = entity_id.split(".")
    er.async_get(hass).async_get_or_create(
        domain, "test", entity_id, suggested_object_id=object_id, device_id=found.id
    )
    return found.id


def player(
    hass: HomeAssistant,
    entity_id: str,
    state: str = "playing",
    features: int = VOLUME,
    **attrs: Any,
) -> None:
    hass.states.async_set(entity_id, state, {"supported_features": int(features), **attrs})


def register_players(hass: HomeAssistant, *, report: bool = True) -> None:
    """media_player actions that change the states (``report=False``: a player that does not
    report its volume back)."""

    async def handle(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        calls.append(
            (call.service, entity_id, {k: v for k, v in call.data.items() if k != "entity_id"})
        )
        state = hass.states.get(entity_id)
        assert state is not None
        attrs, value = dict(state.attributes), state.state
        if call.service == "volume_set":
            attrs["volume_level"] = call.data["volume_level"]
        elif call.service == "volume_mute":
            attrs["is_volume_muted"] = call.data["is_volume_muted"]
        elif call.service == "media_pause":
            value = "paused"
        elif call.service == "media_play":
            value = "playing"
        if report:
            hass.states.async_set(entity_id, value, attrs)

    for service in ("volume_set", "volume_mute", "media_pause", "media_play"):
        hass.services.async_register("media_player", service, handle)


@pytest.fixture
async def ducker(hass: HomeAssistant) -> AsyncIterator[MediaDucker]:
    calls.clear()
    entry = MockConfigEntry(domain="flowpilot", options={CONF_DUCK: True})
    entry.add_to_hass(hass)
    place(hass, SATELLITE, "Гостиная", device="station")
    hass.states.async_set(SATELLITE, "idle")
    found = MediaDucker(hass, entry)
    found.start()
    yield found
    await found.stop()


def set_options(hass: HomeAssistant, ducker: MediaDucker, **options: Any) -> None:
    hass.config_entries.async_update_entry(ducker.entry, options={CONF_DUCK: True, **options})


async def satellite(hass: HomeAssistant, state: str) -> None:
    hass.states.async_set(SATELLITE, state)
    await hass.async_block_till_done(wait_background_tasks=True)


def volume(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.attributes.get("volume_level")


async def test_the_area_gets_quiet_while_the_satellite_listens(
    hass: HomeAssistant, ducker: MediaDucker
) -> None:
    register_players(hass)
    place(hass, "media_player.tv", "Гостиная")
    place(hass, "media_player.station", "Гостиная", device="station")  # the satellite's own
    place(hass, "media_player.kitchen", "Кухня")
    place(hass, "media_player.off_tv", "Гостиная")
    player(hass, "media_player.tv", volume_level=0.5)
    player(hass, "media_player.station", volume_level=0.5)
    player(hass, "media_player.kitchen", volume_level=0.5)
    player(hass, "media_player.off_tv", "off", volume_level=0.5)

    await satellite(hass, "listening")
    assert volume(hass, "media_player.tv") == 0.1  # 20 % by default
    assert [c[1] for c in calls] == ["media_player.tv"]

    await satellite(hass, "processing")  # the command is heard: sound back before it runs
    assert volume(hass, "media_player.tv") == 0.5
    await satellite(hass, "idle")
    assert len(calls) == 2


async def test_players_without_a_volume_are_muted_or_paused(
    hass: HomeAssistant, ducker: MediaDucker
) -> None:
    register_players(hass)
    place(hass, "media_player.tv", "Гостиная")
    player(hass, "media_player.tv", "on", F.VOLUME_MUTE | F.PAUSE)
    await satellite(hass, "listening")
    assert calls[-1] == ("volume_mute", "media_player.tv", {"is_volume_muted": True})
    await satellite(hass, "processing")
    assert calls[-1] == ("volume_mute", "media_player.tv", {"is_volume_muted": False})

    calls.clear()
    set_options(hass, ducker, **{CONF_DUCK_FALLBACK: "pause"})
    player(hass, "media_player.tv", "playing", F.VOLUME_MUTE | F.PAUSE)
    await satellite(hass, "listening")
    assert hass.states.get("media_player.tv").state == "paused"  # type: ignore[union-attr]
    await satellite(hass, "processing")
    assert [c[0] for c in calls] == ["media_pause", "media_play"]

    calls.clear()
    set_options(hass, ducker, **{CONF_DUCK_FALLBACK: "none"})
    await satellite(hass, "listening")
    await satellite(hass, "processing")
    assert calls == []


async def test_options_level_exclusions_and_off(hass: HomeAssistant, ducker: MediaDucker) -> None:
    register_players(hass)
    place(hass, "media_player.tv", "Гостиная")
    place(hass, "media_player.yandex", "Гостиная")
    player(hass, "media_player.tv", volume_level=0.6)
    player(hass, "media_player.yandex", volume_level=0.6)
    set_options(hass, ducker, **{CONF_DUCK_LEVEL: 50, CONF_DUCK_EXCLUDE: ["media_player.yandex"]})
    await satellite(hass, "listening")
    assert volume(hass, "media_player.tv") == 0.3
    assert volume(hass, "media_player.yandex") == 0.6
    await satellite(hass, "idle")

    calls.clear()
    hass.config_entries.async_update_entry(ducker.entry, options={})  # off by default
    await satellite(hass, "listening")
    await satellite(hass, "idle")
    assert calls == []


async def test_a_volume_someone_changed_is_kept(hass: HomeAssistant, ducker: MediaDucker) -> None:
    register_players(hass)
    place(hass, "media_player.tv", "Гостиная")
    player(hass, "media_player.tv", volume_level=0.5)
    await satellite(hass, "listening")
    player(hass, "media_player.tv", volume_level=0.8)  # the remote
    await satellite(hass, "processing")
    assert volume(hass, "media_player.tv") == 0.8

    # a player that never reported our lower volume still gets its own back
    calls.clear()
    hass.services.async_remove("media_player", "volume_set")
    register_players(hass, report=False)
    await satellite(hass, "idle")
    await satellite(hass, "listening")
    await satellite(hass, "processing")
    assert [c[2] for c in calls] == [{"volume_level": 0.16}, {"volume_level": 0.8}]


async def test_flowpilot_acts_after_the_sound_is_back(
    hass: HomeAssistant, ducker: MediaDucker
) -> None:
    """The agent's tools may run while Home Assistant still shows the satellite listening."""
    register_players(hass)
    place(hass, "media_player.tv", "Гостиная")
    player(hass, "media_player.tv", volume_level=0.5)
    seen: list[Any] = []

    async def louder(call: ServiceCall) -> None:
        seen.append(volume(hass, "media_player.tv"))

    hass.services.async_register("media_player", "volume_up", louder)
    host = build_host_ops(hass, ducker.entry, ducker.release)
    await satellite(hass, "listening")
    assert volume(hass, "media_player.tv") == 0.1
    args = {"action": "media_player.volume_up", "target": {"entity_id": "media_player.tv"}}
    result = await host["ha.service.call"](HostRequest(None, {**args, "satellite_id": SATELLITE}))
    assert result["error"] is False
    assert seen == [0.5]
    await satellite(hass, "idle")
    assert len(calls) == 2  # nothing more to give back


async def test_the_options_keep_the_ducking(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="flowpilot", data={"url": "http://x", "token": "t"})
    entry.add_to_hass(hass)
    for domain in ("homeassistant", "conversation", "assist_pipeline"):
        assert await async_setup_component(hass, domain, {})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["data_schema"] is not None
    fields = {str(key) for key in flow["data_schema"].schema}
    assert {CONF_DUCK, CONF_DUCK_LEVEL, CONF_DUCK_FALLBACK, CONF_DUCK_EXCLUDE} <= fields
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {CONF_DUCK: True, CONF_DUCK_LEVEL: 30, CONF_DUCK_FALLBACK: "pause"},
    )
    assert result["type"] == "create_entry"
    assert entry.options == {CONF_DUCK: True, CONF_DUCK_LEVEL: 30, CONF_DUCK_FALLBACK: "pause"}
