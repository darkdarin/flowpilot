"""``play_music``: an LLM tool that plays music with Music Assistant, offered next to Home
Assistant's own tools (which can pause or skip, but not search and start music).

Where: the player the user named, else the Music Assistant player in the area of the device that
heard the request (a satellite), else the only one. Only players exposed to Assist are used.
What: the model passes what was asked, spelling fixed and split into artist and title; Music
Assistant searches every provider (Spotify, radio, ...) and the best match plays at once. Weak
matches come back as choices for the model. Nothing asked for: the library's endless mix.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm

MA_DOMAIN = "music_assistant"
TOOL_NAME = "flowpilot__play_music"
MEDIA_TYPES = ["track", "album", "artist", "playlist", "radio"]
# Below this similarity a result is not taken on trust: the model gets the choices instead.
GOOD_MATCH = 0.6
TYPE_BONUS = {"track": 0.03, "artist": 0.02, "album": 0.0, "playlist": -0.01, "radio": 0.0}
# Music Assistant's own endless mix of the library, for "play some music".
LIBRARY_MIX = "infinite mix (library)"

DESCRIPTION = (
    "Play music with Music Assistant: a song, an artist, an album, a playlist or a radio "
    "station, from any music source. Fix the spelling of names you recognize and pass the artist "
    "and the title separately when you know them. Leave everything empty for 'play some music' "
    "(an endless mix); if the person's music taste is remembered, pass it instead. Without a "
    "player, music plays where the request was heard."
)
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to play as asked, spelling fixed, e.g. 'Кино Группа крови'.",
        },
        "artist": {"type": "string", "description": "The artist or band, if known."},
        "title": {
            "type": "string",
            "description": "The song, album, playlist or radio station name, if known.",
        },
        "media_type": {
            "type": "string",
            "enum": MEDIA_TYPES,
            "description": "What kind of thing was asked for, if clear.",
        },
        "player": {
            "type": "string",
            "description": "Where to play, only if the user named a device or a room.",
        },
        "radio_mode": {
            "type": "boolean",
            "description": "Keep playing similar music after it ends (for 'music like ...').",
        },
    },
}


def music_tools(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The tool, when Music Assistant is set up."""
    if not _ma_entries(hass):
        return []
    return [{"name": TOOL_NAME, "description": DESCRIPTION, "parameters": PARAMETERS}]


def _ma_entries(hass: HomeAssistant) -> list[str]:
    return [
        e.entry_id
        for e in hass.config_entries.async_entries(MA_DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold().replace("ё", "е")).split())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _players(hass: HomeAssistant) -> list[State]:
    """Music Assistant players exposed to Assist and available."""
    registry = er.async_get(hass)
    return [
        state
        for entry in registry.entities.values()
        if entry.platform == MA_DOMAIN
        and entry.domain == "media_player"
        and (state := hass.states.get(entry.entity_id)) is not None
        and state.state != "unavailable"
        and async_should_expose(hass, "conversation", entry.entity_id)
    ]


def _area_of(hass: HomeAssistant, entity_id: str) -> str | None:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    device = dr.async_get(hass).async_get(entry.device_id) if entry.device_id else None
    return device.area_id if device else None


def pick_player(hass: HomeAssistant, wanted: str | None, device_id: str | None) -> State:
    players = _players(hass)
    if not players:
        raise HomeAssistantError("no Music Assistant player is exposed to Assist")
    areas = ar.async_get(hass)

    def label(state: State) -> str:
        area_id = _area_of(hass, state.entity_id)
        area = areas.async_get_area(area_id) if area_id else None
        return f"{state.name} ({area.name})" if area else state.name

    if wanted:
        scored = sorted(
            (
                (max(_sim(wanted, state.name), _sim(wanted, label(state))), state)
                for state in players
            ),
            key=lambda pair: -pair[0],
        )
        # a room's name picks the players in it
        in_room = [
            s
            for s in players
            if (a := _area_of(hass, s.entity_id))
            and (area := areas.async_get_area(a))
            and _sim(wanted, area.name) > 0.75
        ]
        if len(in_room) == 1:
            return in_room[0]
        if scored[0][0] >= GOOD_MATCH:
            return scored[0][1]
        names = ", ".join(label(s) for s in players)
        raise HomeAssistantError(f"no player like {wanted!r}; players: {names}")

    device = dr.async_get(hass).async_get(device_id) if device_id else None
    if device is not None and device.area_id:
        here = [s for s in players if _area_of(hass, s.entity_id) == device.area_id]
        if len(here) == 1:
            return here[0]
        if here:
            # the player of the same device as the satellite, by name (a station and its player)
            best = max(here, key=lambda s: _sim(device.name_by_user or device.name or "", s.name))
            if _sim(device.name_by_user or device.name or "", best.name) >= GOOD_MATCH:
                return best
    if len(players) == 1:
        return players[0]
    names = ", ".join(label(s) for s in players)
    raise HomeAssistantError(f"say where to play; players: {names}")


def _artists(item: dict[str, Any]) -> str:
    return ", ".join(str(a.get("name") or "") for a in item.get("artists") or [])


Ranked = list[tuple[float, str, dict[str, Any]]]


def rank(results: dict[str, Any], args: dict[str, Any]) -> Ranked:
    """Every result with how well it fits what was asked, best first."""
    query = str(args.get("query") or "")
    artist = str(args.get("artist") or "")
    title = str(args.get("title") or "")
    wanted_type = args.get("media_type")
    scored: Ranked = []
    for bucket, items in results.items():
        kind = bucket.rstrip("s") if bucket != "radio" else "radio"
        if kind not in MEDIA_TYPES or (wanted_type and kind != wanted_type):
            continue
        for position, item in enumerate(items if isinstance(items, list) else []):
            name = str(item.get("name") or "")
            by = _artists(item)
            if kind == "artist":
                score = _sim(name, artist or title or query)
                if title:  # a song was asked for, not the whole artist
                    score -= 0.3
            elif title:
                score = _sim(name, title)
                if artist:
                    score = 0.65 * score + 0.35 * (_sim(by, artist) if by else 0.0)
            else:
                score = max(
                    _sim(name, query), _sim(f"{name} {by}", query), _sim(f"{by} {name}", query)
                )
            # ties: a song over the album of the same name, a close artist over a stray album,
            # the household's own library over public look-alikes
            score += TYPE_BONUS[kind]
            if kind == "artist" and not title and score >= 0.85:
                score += 0.08
            if str(item.get("uri") or "").startswith("library://"):
                score += 0.05
            score -= 0.02 * position  # the provider's own order breaks the rest
            scored.append((score, kind, item))
    return sorted(scored, key=lambda t: -t[0])


async def play_music(
    hass: HomeAssistant, llm_context: llm.LLMContext, args: dict[str, Any]
) -> dict[str, Any]:
    entries = _ma_entries(hass)
    if not entries:
        raise HomeAssistantError("Music Assistant is not set up")
    player = pick_player(hass, args.get("player"), llm_context.device_id)
    context = llm_context.context
    asked = any(args.get(k) for k in ("query", "artist", "title"))

    if not asked:
        mix = await _library_mix(hass, entries[0])
        if mix is None:
            raise HomeAssistantError("nothing to play: ask what music to play")
        await _play(hass, player, mix["uri"], "playlist", radio=False, context=context)
        return {"playing": mix["name"], "type": "playlist", "player": player.name}

    data: dict[str, Any] = {
        "config_entry_id": entries[0],
        "name": args.get("title") or args.get("query") or args.get("artist"),
        "limit": 5,
    }
    if args.get("artist") and args.get("title"):
        data["artist"] = args["artist"]
    if args.get("media_type"):
        data["media_type"] = [args["media_type"]]
    response = await hass.services.async_call(
        MA_DOMAIN, "search", data, blocking=True, return_response=True, context=context
    )
    results: dict[str, Any] = dict(response) if isinstance(response, dict) else {}
    if args.get("media_type") in (None, "playlist"):
        # the household's own playlists (and Music Assistant's mixes) are not in a provider search
        own = await _library(hass, entries[0], "playlist", str(data["name"]))
        results["playlists"] = [*own, *(results.get("playlists") or [])]
    ranked = rank(results, args)
    if not ranked:
        return {"error": "nothing found", "searched": data["name"]}
    score, kind, best = ranked[0]
    if score < GOOD_MATCH:
        choices = [
            {"type": k, "name": i.get("name"), "artist": _artists(i) or None}
            for _, k, i in ranked[:4]
        ]
        return {"error": "no good match, ask which one", "choices": choices}
    await _play(
        hass, player, str(best["uri"]), kind, radio=bool(args.get("radio_mode")), context=context
    )
    return {
        "playing": best.get("name"),
        "artist": _artists(best) or None,
        "type": kind,
        "player": player.name,
    }


async def _library(
    hass: HomeAssistant, entry_id: str, media_type: str, search: str
) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        MA_DOMAIN,
        "get_library",
        {"config_entry_id": entry_id, "media_type": media_type, "search": search, "limit": 10},
        blocking=True,
        return_response=True,
    )
    found = response.get("items") if isinstance(response, dict) else None
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []


async def _library_mix(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    items = await _library(hass, entry_id, "playlist", "Infinite Mix")
    for item in items:
        if _norm(str(item.get("name") or "")) == _norm(LIBRARY_MIX):
            return item
    return items[0] if items else None


async def _play(
    hass: HomeAssistant, player: State, uri: str, kind: str, *, radio: bool, context: Any
) -> None:
    await hass.services.async_call(
        MA_DOMAIN,
        "play_media",
        {"media_id": uri, "media_type": kind, "enqueue": "replace", "radio_mode": radio},
        target={"entity_id": player.entity_id},
        blocking=True,
        context=context,
    )
