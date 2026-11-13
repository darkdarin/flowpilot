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


@pytest.fixture(autouse=True)
def _no_known_providers() -> Any:
    music._providers.clear()
    music._last_mix.clear()
    yield
    music._providers.clear()
    music._last_mix.clear()


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
        self.library_tracks = [item("Выхода нет", "library://track/1", "Сплин")]
        self.order: list[str] = []  # stops and plays, in order
        self.hass = hass
        hass.services.async_register("media_player", "media_stop", self.stop)
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
        if call.data["media_type"] == "track":
            return {"items": self.library_tracks}
        mix = item("Infinite Mix (library)", "library://playlist/7")
        return {"items": [item("Infinite Mix (favorites)", "library://playlist/8"), mix]}

    async def stop(self, call: ServiceCall) -> None:
        ids = call.data["entity_id"]
        for entity_id in [ids] if isinstance(ids, str) else ids:
            self.order.append(f"stop {entity_id}")
            # Music Assistant lets go of the player a moment after the stop returns
            self.hass.async_create_task(self.idle(entity_id))

    async def idle(self, entity_id: str) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        self.order.append(f"idle {entity_id}")
        state = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, "idle", state.attributes if state else None)

    async def play(self, call: ServiceCall) -> None:
        ids = call.data["entity_id"]
        self.order.append(f"play {ids if isinstance(ids, str) else ids[0]}")
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
    args = {"name": "flowpilot__play_music", "arguments": arguments, "language": "ru", **turn}
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
    assert ma.played[0]["media_id"] == "library://playlist/7"
    assert result["say"] == "Включаю Infinite Mix (library)"


async def test_my_music_is_the_deezer_flow(hass: HomeAssistant) -> None:
    """Deezer's Flow is in no search and no library: it is the provider's own playlist."""
    host, ma, ids = await setup(hass)
    ma.results = {"tracks": [item("Выхода нет", "deezer--UGbw9F82://track/1", "Сплин")]}
    result = await call(host, {}, device_id=ids["satellite"])
    assert result["error"] is False, result
    assert ma.played[0]["media_id"] == "deezer--UGbw9F82://playlist/flow"
    assert ma.played[0]["media_type"] == "playlist" and result["say"] == "Включаю Flow"
    searches = len(ma.searches)
    await call(host, {}, device_id=ids["satellite"])
    assert len(ma.searches) == searches  # the providers are known now


async def test_other_music_is_another_mix_not_a_search(hass: HomeAssistant) -> None:
    """ "Включи другую музыку" found a song called "Другая музыка": it is no search, but another
    mix than the one that plays, even when the model passes those words as the query."""
    host, ma, ids = await setup(hass)
    ma.results = {"tracks": [item("Другая музыка", "deezer--UGbw9F82://track/9", "Кто-то")]}
    await call(host, {}, device_id=ids["satellite"])  # the person's Flow
    assert ma.played[-1]["media_id"] == "deezer--UGbw9F82://playlist/flow"
    searches = len(ma.searches)
    for args in ({"query": "другую музыку"}, {"different": True}, {"query": "что-нибудь другое"}):
        before = ma.played[-1]["media_id"]
        result = await call(host, args, device_id=ids["satellite"])
        assert result["error"] is False, result
        played = ma.played[-1]["media_id"]
        assert played != "deezer--UGbw9F82://track/9", args  # not the song of that name
        assert played != before, args  # another mix than the one that played
        # after Flow a mix of the library; after that, Flow again
        flow = before.endswith("/playlist/flow")
        assert played.startswith("library://playlist/") if flow else played.endswith("/flow")
    assert len(ma.searches) == searches
    # after a library mix or a song, other music is the person's Flow again
    await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])
    await call(host, {"different": True}, device_id=ids["satellite"])
    assert ma.played[-1]["media_id"] == "deezer--UGbw9F82://playlist/flow"


ROCK = {
    "artists": [item("Рокки", "deezer--x://artist/rocky")],
    "tracks": [item("Рок", "deezer--x://track/rok", "Кто-то")],
    "playlists": [item("Rock Classics", "deezer--x://playlist/rock")],
    "radio": [item("Rock FM", "tunein://radio/rock")],
}


async def test_a_genre_plays_a_playlist_of_it_not_an_artist(hass: HomeAssistant) -> None:
    """ "Включи рок" played the artist "Рокки": a genre plays a playlist of it."""
    host, ma, ids = await setup(hass)
    ma.results = ROCK
    asked = ({"query": "рок"}, {"genre": "rock"}, {"query": "русский рок"}, {"query": "рок-музыку"})
    for args in asked:
        result = await call(host, args, device_id=ids["satellite"])
        assert result["error"] is False, (args, result)
        assert ma.played[-1]["media_id"] == "deezer--x://playlist/rock", args
        assert ma.played[-1]["media_type"] == "playlist"
        assert result["say"] == "Включаю Rock Classics"
    # the search looked for playlists and radio only
    looked = [s for s in ma.searches if s["name"] != "music"]  # not the look for providers
    assert all(s.get("media_type") == ["playlist", "radio"] for s in looked)
    # a band with a genre-like name is still found by name
    ma.results = SPLIN
    await call(host, {"artist": "Сплин", "title": "Выхода нет"}, device_id=ids["satellite"])
    assert ma.played[-1]["media_id"] == "spotify://track/splin"


async def test_a_genre_plays_the_playlist_named_closest_to_it(hass: HomeAssistant) -> None:
    """As the owner's Music Assistant answered: "90s Pop Rock" came first, "Рок" later."""
    host, ma, ids = await setup(hass)
    playlists = ["90s Pop Rock", "Rock Essentials", "Рок балади", "Рок"]
    ma.results = {
        "playlists": [item(n, f"deezer--x://playlist/{i}") for i, n in enumerate(playlists)]
    }
    await call(host, {"query": "рок"}, device_id=ids["satellite"])
    assert ma.played[-1]["media_id"] == "deezer--x://playlist/3"  # named just the genre
    ma.results = {
        "playlists": [item(n, f"deezer--x://playlist/{i}") for i, n in enumerate(playlists[:2])]
    }
    await call(host, {"genre": "rock"}, device_id=ids["satellite"])
    assert ma.played[-1]["media_id"] == "deezer--x://playlist/1"  # starts with it


async def test_a_genre_without_a_playlist_of_it_asks(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    ma.results = {"artists": [item("Рокки", "deezer--x://artist/rocky")], "playlists": []}
    result = await call(host, {"query": "рок"}, device_id=ids["satellite"])
    assert result["error"] is True and "ask what to play" in result["data"]["error"]
    assert all(c["name"] != "Рокки" for c in result["data"]["choices"])  # no artist among them
    assert not ma.played


async def test_just_music_is_no_search(hass: HomeAssistant) -> None:
    host, ma, ids = await setup(hass)
    result = await call(host, {"query": "музыку"}, device_id=ids["satellite"])
    assert result["error"] is False and ma.played[0]["media_id"] == "library://playlist/7"
    assert all(s["name"] != "музыку" for s in ma.searches)  # only the look for providers


async def test_an_empty_library_has_no_mix_to_play(hass: HomeAssistant) -> None:
    """Music Assistant answers "no playable items" and drops the queue that was playing."""
    host, ma, ids = await setup(hass)
    ma.library_tracks = []
    result = await call(host, {}, device_id=ids["satellite"])
    assert result["error"] is True and "nothing to play" in result["data"]["message"]
    assert not ma.played


async def test_the_answer_has_words_for_the_person(hass: HomeAssistant) -> None:
    host, _, ids = await setup(hass)
    result = await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])
    assert result["say"] == "Включаю Сплин — Выхода нет" and "say" not in result["data"]
    weak = await call(host, {"query": "что-то совсем другое"}, device_id=ids["satellite"])
    assert "say" not in weak  # the model asks which one


async def test_playing_music_is_stopped_before_the_new_one(hass: HomeAssistant) -> None:
    """Replacing music that plays loses the new music on a Sendspin player: Music Assistant
    clears the player's output once the old music stops, after the new one started."""
    host, ma, ids = await setup(hass)
    hass.states.async_set(ids["station"], "playing", {"friendly_name": "Яндекс Станция Миди"})
    await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])
    await hass.async_block_till_done(wait_background_tasks=True)
    station = ids["station"]
    assert ma.order == [f"stop {station}", f"idle {station}", f"play {station}"]
    ma.order.clear()
    await call(host, {"query": "сплин выхода нет"}, device_id=ids["satellite"])  # idle now
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ma.order == [f"play {ids['station']}"]


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
