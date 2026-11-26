"""Timers from a turn without a speaker of its own (a Telegram chat, the Assist chat).

Home Assistant's timers belong to a device that rings them: a voice satellite that registered for
timers. A typed turn has no device, so Home Assistant refuses its timers ("Device does not support
timers"). Here a timer tool of such a turn gets a ``device`` argument: the agent names the speaker
(its name or room); not named, the speaker chosen in the integration's options; none chosen and
only one speaker able to ring timers, that one; else the agent is asked to say which.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.intent import async_device_supports_timers
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent

from .music import GOOD_MATCH, _sim

TIMER_INTENTS = frozenset(
    {
        intent.INTENT_START_TIMER,
        intent.INTENT_CANCEL_TIMER,
        intent.INTENT_CANCEL_ALL_TIMERS,
        intent.INTENT_INCREASE_TIMER,
        intent.INTENT_DECREASE_TIMER,
        intent.INTENT_PAUSE_TIMER,
        intent.INTENT_UNPAUSE_TIMER,
        intent.INTENT_TIMER_STATUS,
    }
)
DEVICE_ARG = "device"
# the entities of a speaker whose aliases name the speaker itself
SPEAKER_DOMAINS = frozenset({"assist_satellite", "media_player"})


def is_timer_tool(name: str) -> bool:
    """A timer tool of the Assist API ("HassStartTimer", or namespaced "intent__HassStartTimer")."""
    return name.rsplit("__", 1)[-1] in TIMER_INTENTS


def rings_timers(hass: HomeAssistant, device_id: str | None) -> bool:
    return bool(device_id) and async_device_supports_timers(hass, str(device_id))


def timer_devices(hass: HomeAssistant) -> dict[str, str]:
    """The devices that ring timers: device id → "Name (Area)"."""
    areas = ar.async_get(hass)
    registry = dr.async_get(hass)
    found: dict[str, str] = {}
    # Home Assistant 2026.11 iterates the registry as device entries, 2026.10 as their ids
    for item in list(registry.devices):
        device = item if isinstance(item, dr.DeviceEntry) else registry.async_get(item)
        if device is None or device.disabled:
            continue
        if not async_device_supports_timers(hass, device.id):
            continue
        name = device.name_by_user or device.name or device.id
        area = areas.async_get_area(device.area_id) if device.area_id else None
        found[device.id] = f"{name} ({area.name})" if area else name
    return found


def device_aliases(hass: HomeAssistant, device_id: str | None) -> list[str]:
    """What people call a speaker besides its name ("Алиса"): the aliases of its satellite and
    media player entities in Home Assistant."""
    if not device_id:
        return []
    found: list[str] = []
    for entry in er.async_entries_for_device(er.async_get(hass), device_id):
        if entry.domain in SPEAKER_DOMAINS:
            # 2026.11 keeps the entity's own name in the list as a marker, not a string
            found += [a for a in entry.aliases if isinstance(a, str) and a not in found]
    return found


def speaker_names(hass: HomeAssistant, devices: dict[str, str]) -> list[str]:
    """The speakers' labels, then their aliases: the values the agent may name them by."""
    labels = sorted(devices.values())
    aliases = sorted({a for d in devices for a in device_aliases(hass, d)} - set(labels))
    return labels + aliases


def with_device_argument(spec: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    """A timer tool's definition with ``device``: which speaker the timer is on."""
    parameters = dict(spec.get("parameters") or {"type": "object"})
    properties = dict(parameters.get("properties") or {})
    properties[DEVICE_ARG] = {
        "type": "string",
        "enum": labels,
        "description": (
            "The speaker the timer rings on (this conversation has no speaker of its own): the "
            "one the person named, by its name, another name of it or its room. Leave it out "
            "when they did not name one."
        ),
    }
    parameters["properties"] = properties
    return {**spec, "parameters": parameters}


def timer_device(hass: HomeAssistant, wanted: str | None, default: str | None) -> str:
    """The device a timer of a turn without a speaker goes to (see the module's docstring)."""
    devices = timer_devices(hass)
    if not devices:
        raise HomeAssistantError("no speaker in this home can ring timers")
    if wanted:
        exact = [
            d
            for d, label in devices.items()
            if wanted.casefold() in {n.casefold() for n in [label, *device_aliases(hass, d)]}
        ]
        if exact:
            return exact[0]
        best = max(devices, key=lambda d: _similarity(hass, wanted, d, devices[d]))
        if _similarity(hass, wanted, best, devices[best]) >= GOOD_MATCH:
            return best
        raise HomeAssistantError(
            f"no speaker like {wanted!r} rings timers; they are: {', '.join(devices.values())}"
        )
    if default and default in devices:
        return default
    if len(devices) == 1:
        return next(iter(devices))
    raise HomeAssistantError(
        "this conversation has no speaker of its own: ask which speaker the timer is on "
        f"({', '.join(devices.values())}) and pass it as {DEVICE_ARG}"
    )


def _similarity(hass: HomeAssistant, wanted: str, device_id: str, label: str) -> float:
    device = dr.async_get(hass).async_get(device_id)
    area = ar.async_get(hass).async_get_area(device.area_id) if device and device.area_id else None
    names = [label, *([area.name] if area else []), *device_aliases(hass, device_id)]
    if device is not None:
        names.append(device.name_by_user or device.name or "")
    return max(_sim(wanted, n) for n in names)
