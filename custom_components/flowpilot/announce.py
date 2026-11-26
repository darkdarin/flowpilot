"""Say a text on chosen speakers: FlowPilot's own tool for the agents.

Home Assistant's broadcast (``HassBroadcast``) says a message on every speaker but the one that
heard the request; this tool says it on the speakers the person named, by name or room (from a
Telegram chat: "скажи на кухне, что обед готов"); a home with one speaker needs no name. It
uses ``assist_satellite.announce``, so any voice satellite that can announce works, a Yandex
Station among them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .music import GOOD_MATCH, _area_of, _sim
from .timers import device_aliases

TOOL_NAME = "flowpilot__announce"
SATELLITE = "assist_satellite"
DESCRIPTION = (
    "Say a text aloud on the speakers the person named (by name or room), e.g. 'tell the "
    "kitchen dinner is ready'. Write the message as it should be heard. For every speaker at "
    "once use the broadcast tool instead."
)


def speakers(hass: HomeAssistant) -> dict[str, str]:
    """Voice satellites that can announce: entity id → "Name (Area)"."""
    registry = er.async_get(hass)
    areas = ar.async_get(hass)
    found: dict[str, str] = {}
    for entity_id in hass.states.async_entity_ids(SATELLITE):
        entry = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        if entry is None or state is None or state.state == STATE_UNAVAILABLE:
            continue
        if not (entry.supported_features or 0) & AssistSatelliteEntityFeature.ANNOUNCE:
            continue
        area_id = _area_of(hass, entity_id)
        area = areas.async_get_area(area_id) if area_id else None
        found[entity_id] = f"{state.name} ({area.name})" if area else state.name
    return found


def aliases(hass: HomeAssistant, entity_id: str) -> list[str]:
    """What people call the speaker besides its name ("Алиса"): the aliases of its satellite and
    media player in Home Assistant."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return []
    if entry.device_id:
        return device_aliases(hass, entry.device_id)
    return [a for a in entry.aliases if isinstance(a, str)]


def names(hass: HomeAssistant, found: dict[str, str]) -> list[str]:
    """The speakers' labels, then their aliases: the values the agent may name them by."""
    labels = sorted(found.values())
    return labels + sorted({a for e in found for a in aliases(hass, e)} - set(labels))


def announce_tools(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The tool, when some speaker can announce."""
    found = speakers(hass)
    if not found:
        return []
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to say."},
            "speakers": {
                "type": "array",
                "items": {"type": "string", "enum": names(hass, found)},
                "description": (
                    "The speakers to say it on, by name, another name or room (a room means "
                    "its speakers). Leave it out when "
                    "the person did not say where and there is only one speaker."
                ),
            },
        },
        "required": ["message"],
    }
    return [{"name": TOOL_NAME, "description": DESCRIPTION, "parameters": parameters}]


def pick(hass: HomeAssistant, wanted: list[str]) -> list[str]:
    """The speakers' entity ids for what the agent named: a speaker, or a room's speakers."""
    found = speakers(hass)
    if not found:
        raise HomeAssistantError("no speaker in this home can announce")
    areas = ar.async_get(hass)
    picked: list[str] = []
    for name in wanted:
        exact = [
            e
            for e, label in found.items()
            if name.casefold() in {n.casefold() for n in [label, *aliases(hass, e)]}
        ]
        in_room = [
            e
            for e in found
            if (area_id := _area_of(hass, e))
            and (area := areas.async_get_area(area_id))
            and _sim(name, area.name) > 0.75
        ]

        def similarity(entity_id: str, name: str = name) -> float:
            return max(_sim(name, n) for n in [found[entity_id], *aliases(hass, entity_id)])

        scored = max(found, key=similarity)
        if exact or in_room:
            picked += exact or in_room
        elif similarity(scored) >= GOOD_MATCH:
            picked.append(scored)
        else:
            raise HomeAssistantError(
                f"no speaker like {name!r}; they are: {', '.join(sorted(found.values()))}"
            )
    return list(dict.fromkeys(picked))


async def announce(
    hass: HomeAssistant, context: Context | None, args: dict[str, Any]
) -> dict[str, Any]:
    """Start saying the message on the named speakers; the answer comes before they finish."""
    message = " ".join(str(args.get("message") or "").split())
    if not message:
        raise ValueError("say what to announce")
    wanted = args.get("speakers") or []
    if isinstance(wanted, str):
        wanted = [wanted]
    found = speakers(hass)
    if wanted:
        targets = pick(hass, [str(w) for w in wanted])
    elif len(found) == 1:  # the only speaker of the home: "say it on the speaker"
        targets = list(found)
    else:
        raise ValueError(f"say on which speakers: {', '.join(sorted(found.values()))}")
    await hass.services.async_call(
        SATELLITE,
        "announce",
        {"message": message},
        target={"entity_id": targets},
        blocking=False,
        context=context,
    )
    return {"announcing_on": [found[t] for t in targets]}
