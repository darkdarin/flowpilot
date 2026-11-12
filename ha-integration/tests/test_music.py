"""``flowpilot__play_music``: Music Assistant search and play for the LLM agent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.flowpilot import music
from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.host import build_host_ops
from custom_components.flowpilot.music import rank


def item(name: str, uri: str, *artists: str) -> dict[str, Any]:
    return {"name": name, "uri": uri, "artists": [{"name": a} for a in artists]}


# shaped like Music Assistant's answer to "сплин выхода нет" (Spotify, owner's HA, 2026-11-07)
SPLIN = {
    "artists": [item("Сплин", "spotify://artist/splin"), item("Кино", "spotify://artist/kino")],
    "albums": [item("выхода нет", "spotify://album/x", "ksmfx")],
    "tracks": [
        item("Выхода нет", "spotify://track/splin", "Сплин"),
        item("Взрослые", "spotify://track/oqjav", "OQJAV"),
    ],
    "playlists": [item("Сплин — Выхода нет", "spotify://playlist/p")],
    "radio": [],
}


def test_ranking_prefers_what_was_asked() -> None:
    assert rank(SPLIN, {"query": "сплин выхода нет"})[0][2]["uri"] == "spotify://track/splin"
    by_parts = rank(SPLIN, {"artist": "Сплин", "title": "Выхода нет"})
    assert by_parts[0][2]["uri"] == "spotify://track/splin"
    artist = rank(SPLIN, {"query": "Сплин", "media_type": "artist"})
    assert artist[0][1] == "artist" and artist[0][2]["name"] == "Сплин"
    metallica = {"artists": [item("Metallica", "spotify://artist/m")], "tracks": []}
    assert rank(metallica, {"query": "Metalica"})[0][0] > 0.8  # a typo still matches


class FakeMusicAssistant:
    def __init__(self, hass: HomeAssistant) -> None:
        self.searches: list[dict[str, Any]] = []
        self.played: list[dict[str, Any]] = []
        self.results: dict[str, Any] = SPLIN
        self.starting = asyncio.Event()  # set: play_media returns (the music started)
        self.starting.set()
        self.fail: Exception | None = None
        hass.services.async_register(
            "music_assistant", "search", self.search, supports_response=SupportsResponse.ONLY
        )
        hass.services.async_register(
            "music_assistant",
            "get_library",
            self.library,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register("music_assistant", "play_media", self.play)

    async def search(self, call: ServiceCall) -> dict[str, Any]:
        self.searches.append(dict(call.data))
        return self.results

    async def library(self, call: ServiceCall) -> dict[str, Any]:
        mix = item("Infinite Mix (library)", "library://playlist/7")
        return {"items": [item("Infinite Mix (favorites)", "library://playlist/8"), mix]}

    async def play(self, call: ServiceCall) -> None:
        self.played.append({**call.data, "context_user": call.context.user_id})
        await self.starting.wait()
        if self.fail is not None:
            raise self.fail


async def setup(hass: HomeAssistant) -> tuple[Any, FakeMusicAssistant, dict[str, str]]:
    for domain in ("homeassistant", "llm", "conversation", "intent"):
        assert await async_setup_component(hass, domain, {})
    mock_integration(hass, MockModule("music_assistant"))  # the real one needs its client
    ma_entry = MockConfigEntry(domain="music_assistant", title="Music Assistant")
    ma_entry.add_to_hass(hass)
    ma_entry.mock_state(hass, ConfigEntryState.LOADED)
    fp_entry = MockConfigEntry(domain="flowpilot")
    fp_entry.add_to_hass(hass)
    living = ar.async_get(hass).async_create("Гостиная")
    kitchen = ar.async_get(hass).async_create("Кухня")
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    ids: dict[str, str] = {}

    def device(name: str, area: str, key: str) -> str:
        d = devices.async_get_or_create(
            config_entry_id=fp_entry.entry_id, identifiers={("test", key)}, name=name
        )
        devices.async_update_device(d.id, area_id=area)
        return d.id

    ids["satellite"] = device("Яндекс Станция Миди", living.id, "sat")
    for key, name, area in (
        ("station", "Яндекс Станция Миди", living.id),
        ("tv", "Телевизор в гостиной", living.id),
        ("kitchen", "Колонка на кухне", kitchen.id),
        ("hidden", "Скрытая колонка", kitchen.id),
    ):
        entry = entities.async_get_or_create(
            "media_player",
            "music_assistant",
            key,
            config_entry=ma_entry,
            device_id=device(name, area, f"ma-{key}"),
            original_name=None,
        )
        hass.states.async_set(entry.entity_id, "idle", {"friendly_name": name})
        async_expose_entity(hass, "conversation", entry.entity_id, key != "hidden")
        ids[key] = entry.entity_id
    ma = FakeMusicAssistant(hass)
    return build_host_ops(hass, fp_entry), ma, ids


async def call(host: Any, arguments: dict[str, Any], **turn: Any) -> dict[str, Any]:
    args = {"name": "flowpilot__play_music", "arguments": arguments, **turn}
    result: dict[str, Any] = await host["ha.llm.call"](HostRequest(None, args))
    return result


async def test_the_tool_is_offered_with_music_assistant(hass: HomeAssistant) -> None:
    host, _, _ = await setup(hass)
    tools = await host["ha.llm.tools"](HostRequest(None, {}))
    assert "flowpilot__play_music" in {t["name"] for t in tools["tools"]}


async def test_plays_the_best_match_where_it_was_asked(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    user = await hass.auth.async_create_user("Pavel")
    result = await call(
        host, {"query": "сплин выхода нет"}, device_id=ids["satellite"], user_id=user.id
    )
    assert result["error"] is False, result
    assert result["data"]["playing"] == "Выхода нет" and result["data"]["artist"] == "Сплин"
    [played] = ma.played
    # the station's own player, not the TV in the same room
    assert played["entity_id"] == ids["station"]
    assert played["media_id"] == "spotify://track/splin" and played["media_type"] == "track"
    assert played["radio_mode"] is True  # similar music goes on after the song
    assert played["context_user"] == user.id


async def test_a_named_room_or_player_wins(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    await call(host, {"query": "сплин выхода нет", "player": "кухня"}, device_id=ids["satellite"])
    await call(host, {"query": "сплин выхода нет", "player": "телевизор"})
    assert [p["entity_id"] for p in ma.played] == [ids["kitchen"], ids["tv"]]


async def test_without_a_place_and_several_players_it_asks(hass: HomeAssistant) -> None:
    host, ma, _ = await setup(hass)
    result = await call(host, {"query": "сплин выхода нет"})  # from a chat: no device
    assert result["error"] is True and "say where" in result["data"]["message"]
    assert "Скрытая" not in result["data"]["message"]  # not exposed to Assist
    assert not ma.played


async def test_nothing_asked_plays_the_library_mix(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    result = await call(host, {}, device_id=ids["satellite"])
    assert result["data"]["playing"] == "Infinite Mix (library)"
    assert ma.played[0]["media_id"] == "library://playlist/7" and not ma.searches


async def test_a_weak_match_comes_back_as_choices(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    result = await call(host, {"query": "что-то совсем другое"}, device_id=ids["satellite"])
    assert result["error"] is True and result["data"]["choices"]
    assert not ma.played


async def test_only_this_song_when_asked(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    await call(host, {"query": "сплин выхода нет", "radio_mode": False}, device_id=ids["satellite"])
    await call(host, {}, device_id=ids["satellite"])  # the library mix goes on by itself
    assert [p["radio_mode"] for p in ma.played] == [False, False]


async def test_a_slow_start_does_not_hold_the_turn(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Spotify can make Music Assistant wait half a minute: the tool answers that the music is
    starting, the music goes on, and a later failure is logged."""
    monkeypatch.setattr(music, "START_WAIT_S", 0.05)
    host, ma, ids = await setup(hass)
    ma.starting.clear()
    result = await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])
    assert result["error"] is False and result["data"]["status"] == music.STARTING
    assert result["data"]["playing"] == "Выхода нет"
    ma.fail = HomeAssistantError("Spotify Rate Limiter")
    ma.starting.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "did not play spotify://track/splin" in caplog.text


async def test_a_quick_failure_reaches_the_model(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    ma.fail = HomeAssistantError("player is off")
    result = await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])
    assert result["error"] is True and "player is off" in result["data"]["message"]
