"""``play_music``: an LLM tool that plays music with Music Assistant, offered next to Home
Assistant's own tools (which can pause or skip, but not search and start music).

Where: the player the user named, else the Music Assistant player in the area of the device that
heard the request (a satellite), else the only one. Only players exposed to Assist are used.
What: the model passes what was asked, spelling fixed and split into artist and title; Music
Assistant searches every provider (Spotify, radio, ...) and the best match plays at once. Weak
matches come back as choices for the model. Nothing asked for: the person's own stream of a
provider that has one (Deezer Flow), else the library's endless mix.

Starting can take long (an artist's mix is fetched track by track; Spotify makes Music Assistant's
shared client ID wait 10-45 s). The tool waits a moment for a quick failure, then answers that the
music is starting and lets it go on: the turn does not hang, and a failure later is logged. The
answer carries ``say``, the words for the person, so the agent need not ask the model for them.

"Other music" ("включи другую музыку", "что-нибудь другое") is not a search: another mix plays,
not the one this tool started last on the player (the person's stream, the library mix, a library
playlist). The model says so with ``different``; a query of only such words counts as it too.

Music that already plays is stopped first, and the new music starts once Music Assistant has let
go of the player: replacing it at once loses the new music on some players (Music Assistant 2.10
clears the player's output once the old music stops, after the new one started).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from difflib import SequenceMatcher
from typing import Any

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

MA_DOMAIN = "music_assistant"
TOOL_NAME = "flowpilot__play_music"
MEDIA_TYPES = ["track", "album", "artist", "playlist", "radio"]
# Below this similarity a result is not taken on trust: the model gets the choices instead.
GOOD_MATCH = 0.6
TYPE_BONUS = {"track": 0.03, "artist": 0.02, "album": 0.0, "playlist": -0.01, "radio": 0.0}
# Music Assistant's own endless mix of the library, for "play some music".
LIBRARY_MIX = "infinite mix (library)"
# How long the tool waits for the music to start (or fail at once) before it answers that it is
# starting
START_WAIT_S = 1.0
STARTING = "starting: the music begins in a few seconds"
# How long playing music may take to stop before the new music starts anyway
STOP_WAIT_S = 2.0
# A provider's own endless stream of the person's music, by provider domain
PERSONAL_STREAM = {"deezer": "playlist/flow"}
# Provider instances seen in search results (``deezer--UGbw9F82``), by Music Assistant entry
_providers: dict[str, set[str]] = {}
# The mix this tool started last, by player: "other music" is not that one
_last_mix: dict[str, str] = {}
# Words of a request for "other music" rather than for something by name
OTHER_WORDS = {
    "другой",
    "другая",
    "другую",
    "другое",
    "другие",
    "иной",
    "иную",
    "иное",
    "новую",
    "новая",
    "смени",
    "поменяй",
    "other",
    "different",
    "else",
    "another",
    "new",
}
GENERIC_WORDS = OTHER_WORDS | {
    "музыка",
    "музыку",
    "музычку",
    "песню",
    "песня",
    "песни",
    "что",
    "нибудь",
    "то",
    "что-нибудь",
    "что-то",
    "какую",
    "какой",
    "music",
    "something",
    "some",
    "song",
    "songs",
    "track",
    "play",
}
SAY = {
    "ru": "Включаю {}",
    "en": "Playing {}",
}

DESCRIPTION = (
    "Play music with Music Assistant: a song, an artist, an album, a playlist or a radio "
    "station, from any music source. Fix the spelling of names you recognize and pass the artist "
    "and the title separately when you know them. Leave everything empty for 'play some music', "
    "'my music' or 'my wave': the person's own endless stream plays (Deezer Flow, or the "
    "library mix). For 'other music' or 'something else' set different, do not search for "
    "those words. Without a player, music plays where the request was heard. A status "
    "'starting' means it is on its way: say what will play, e.g. 'Включаю Сплин', not that it "
    "failed."
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
        "different": {
            "type": "boolean",
            "description": (
                "True for 'other music', 'something else', 'change the music' ('включи другую "
                "музыку', 'что-нибудь другое'): another mix than the one playing, not a search. "
                "Leave query empty then."
            ),
        },
        "radio_mode": {
            "type": "boolean",
            "description": (
                "Keep playing similar music after it (on by default for a song, an artist or an "
                "album); false only when asked for just this one."
            ),
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
    different = bool(args.get("different")) or _asks_for_other(args)
    asked = (
        not different
        and any(args.get(k) for k in ("query", "artist", "title"))
        and not _only_generic(args)
    )

    language = llm_context.language or hass.config.language

    if not asked:
        mix = (
            await _other_mix(hass, entries[0], _last_mix.get(player.entity_id))
            if different
            else await _own_stream(hass, entries[0])
        )
        if mix is None:
            raise HomeAssistantError("nothing to play: ask what music to play")
        try:
            started = await _play(
                hass, player, mix["uri"], "playlist", radio=False, context=context
            )
        except HomeAssistantError:
            _providers.pop(entries[0], None)  # a provider may be gone: look again next time
            raise
        _last_mix[player.entity_id] = str(mix["uri"])
        return {
            "playing": mix["name"],
            "type": "playlist",
            "player": player.name,
            **started,
            "say": say(language, mix["name"]),
        }

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
    _seen(entries[0], results)
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
    started = await _play(
        hass, player, str(best["uri"]), kind, radio=radio_for(kind, args), context=context
    )
    _last_mix[player.entity_id] = str(best["uri"])  # "other music" next: not this
    by = _artists(best)
    name = str(best.get("name") or "")
    return {
        "playing": best.get("name"),
        "artist": by or None,
        "type": kind,
        "player": player.name,
        **started,
        "say": say(language, f"{by} — {name}" if by and kind in ("track", "album") else name),
    }


def _words_of(args: dict[str, Any]) -> list[str]:
    text = " ".join(str(args.get(k) or "") for k in ("query", "artist", "title"))
    return _norm(text).split()


def _only_generic(args: dict[str, Any]) -> bool:
    """The request names nothing ("музыку", "что-нибудь"): it is no search."""
    words = _words_of(args)
    return bool(words) and all(w in GENERIC_WORDS for w in words)


def _asks_for_other(args: dict[str, Any]) -> bool:
    """A request of only generic words that asks for something else ("другую музыку")."""
    words = _words_of(args)
    return _only_generic(args) and any(w in OTHER_WORDS for w in words)


async def _other_mix(hass: HomeAssistant, entry_id: str, last: str | None) -> dict[str, Any] | None:
    """Another mix than ``last``: after the person's stream, the library mix or a playlist of the
    library (at random); after anything else, the person's stream."""
    own = await _own_stream(hass, entry_id)
    if own is not None and own["uri"] != last and _is_personal(str(own["uri"])):
        return own
    candidates = [own] if own is not None else []
    candidates += await _library(hass, entry_id, "playlist", "")
    others: list[dict[str, Any]] = []
    for candidate in candidates:
        uri = str(candidate.get("uri") or "")
        if (
            uri
            and uri != last
            and not _is_personal(uri)
            and uri not in {str(o["uri"]) for o in others}
        ):
            others.append(candidate)
    if not others:
        return own if own is not None and own["uri"] != last else None
    mix = next((c for c in others if _norm(str(c.get("name") or "")) == _norm(LIBRARY_MIX)), None)
    # the library mix first, then a playlist at random
    return mix if mix is not None and random.random() < 0.5 else random.choice(others)


def _is_personal(uri: str) -> bool:
    """A provider's own stream of the person (Deezer Flow)."""
    return any(uri.endswith(f"://{path}") for path in PERSONAL_STREAM.values())


def say(language: str | None, what: str) -> str:
    """What to tell the person once the music is on its way: 'Включаю Сплин — Выхода нет'."""
    base = (language or "").split("-")[0].casefold()
    return SAY.get(base, SAY["en"]).format(what).strip()


async def _library(
    hass: HomeAssistant, entry_id: str, media_type: str, search: str
) -> list[dict[str, Any]]:
    response = await hass.services.async_call(
        MA_DOMAIN,
        "get_library",
        {
            "config_entry_id": entry_id,
            "media_type": media_type,
            **({"search": search} if search else {}),
            "limit": 10,
        },
        blocking=True,
        return_response=True,
    )
    found = response.get("items") if isinstance(response, dict) else None
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []


# after these, music goes on with similar tracks unless asked otherwise; a playlist and a radio
# station go on by themselves
RADIO_BY_DEFAULT = {"track", "artist", "album"}


def radio_for(kind: str, args: dict[str, Any]) -> bool:
    asked = args.get("radio_mode")
    return bool(asked) if asked is not None else kind in RADIO_BY_DEFAULT


def _seen(entry_id: str, results: dict[str, Any]) -> None:
    """Remember the provider instances of the results (the scheme of their URIs)."""
    for items in results.values():
        for item in items if isinstance(items, list) else []:
            uri = str(item.get("uri") or "") if isinstance(item, dict) else ""
            scheme = uri.partition("://")[0]
            if scheme and scheme != "library":
                _providers.setdefault(entry_id, set()).add(scheme)


async def _own_stream(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """The person's own endless music: a provider's personal stream (Deezer Flow), else the
    library mix when the library has tracks, else None."""
    if not _providers.get(entry_id):
        # no search yet since Home Assistant started: any search shows the providers
        response = await hass.services.async_call(
            MA_DOMAIN,
            "search",
            {"config_entry_id": entry_id, "name": "music", "limit": 1},
            blocking=True,
            return_response=True,
        )
        _seen(entry_id, dict(response) if isinstance(response, dict) else {})
    for instance in sorted(_providers.get(entry_id, ())):
        domain = instance.partition("--")[0]
        if path := PERSONAL_STREAM.get(domain):
            return {"name": "Flow", "uri": f"{instance}://{path}"}
    if not await _library(hass, entry_id, "track", ""):
        return None  # the mix of an empty library has nothing to play
    items = await _library(hass, entry_id, "playlist", "Infinite Mix")
    for item in items:
        if _norm(str(item.get("name") or "")) == _norm(LIBRARY_MIX):
            return item
    return items[0] if items else None


async def _play(
    hass: HomeAssistant, player: State, uri: str, kind: str, *, radio: bool, context: Any
) -> dict[str, Any]:
    """Start playing; ``{}`` once it started, ``{"status": STARTING}`` when it is still starting
    after ``START_WAIT_S`` (it goes on, a failure is logged). A failure before that raises."""

    async def start() -> None:
        if (state := hass.states.get(player.entity_id)) is not None and state.state == "playing":
            await _stop(hass, player.entity_id, context)
        await hass.services.async_call(
            MA_DOMAIN,
            "play_media",
            {"media_id": uri, "media_type": kind, "enqueue": "replace", "radio_mode": radio},
            target={"entity_id": player.entity_id},
            blocking=True,
            context=context,
        )

    task = hass.async_create_background_task(start(), f"flowpilot play_music {uri}")
    done, _ = await asyncio.wait({task}, timeout=START_WAIT_S)
    if done:
        task.result()  # a failure goes to the model
        return {}

    def finished(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and (err := task.exception()) is not None:
            _LOGGER.warning("Music Assistant did not play %s on %s: %s", uri, player.name, err)

    task.add_done_callback(finished)
    return {"status": STARTING}


async def _stop(hass: HomeAssistant, entity_id: str, context: Any) -> None:
    """Stop the music and wait until the player no longer plays (``STOP_WAIT_S`` at most)."""
    stopped = asyncio.Event()

    @callback
    def changed(event: Any) -> None:
        new = event.data.get("new_state")
        if new is None or new.state != "playing":
            stopped.set()

    unsubscribe = async_track_state_change_event(hass, [entity_id], changed)
    try:
        await hass.services.async_call(
            "media_player",
            "media_stop",
            target={"entity_id": entity_id},
            blocking=True,
            context=context,
        )
        if (state := hass.states.get(entity_id)) is None or state.state != "playing":
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(STOP_WAIT_S):
                await stopped.wait()
    finally:
        unsubscribe()
