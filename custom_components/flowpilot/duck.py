"""Quiet the media players in a satellite's area while it listens to a command.

A television or music next to a satellite is the loudest competing voice it hears, and speech
recognition cannot take it out. Commercial speakers that drive their own TV turn it down while they
listen; we do the same with Home Assistant's media players. An assist satellite turns ``listening``
on its pipeline's STT start, right after the wake word, and leaves it when the command is heard
(``AssistSatelliteEntity``, Home Assistant 2026.11). While it listens, the playing media players of
its area (except its own device and the excluded ones) get a lower volume; players without a volume
are muted or paused, as the owner chose.

The players get their volume back as soon as the satellite stops listening, before the command
runs, so "turn the TV down" counts from the real volume. FlowPilot also waits for that before it
acts on a turn (``release``): its tools may run while Home Assistant still shows the satellite
listening. A player whose volume someone changed meanwhile keeps the new one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.media_player.const import (
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    EVENT_STATE_CHANGED,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_DUCK,
    CONF_DUCK_EXCLUDE,
    CONF_DUCK_FALLBACK,
    CONF_DUCK_LEVEL,
    DUCK_LEVEL_DEFAULT,
    DUCK_MUTE,
    DUCK_PAUSE,
)
from .host import area_of, entity_area

_LOGGER = logging.getLogger(__name__)

MEDIA_PLAYER = "media_player"
SATELLITE_DOMAIN = "assist_satellite"
LISTENING = "listening"  # AssistSatelliteState.LISTENING
SOUNDING = {MediaPlayerState.PLAYING, MediaPlayerState.ON, MediaPlayerState.BUFFERING}
# A satellite that listens longer than this is stuck: the players get their sound back anyway.
MAX_DUCK_S = 60.0
CALL_TIMEOUT_S = 3.0
# How long a release waits for the players to report their restored volume.
SETTLE_TIMEOUT_S = 1.5
SETTLE_STEP_S = 0.1
# Players round volumes to their own steps.
VOLUME_TOLERANCE = 0.03


@dataclass
class Ducked:
    """One player quieted for a satellite, and how to give its sound back."""

    entity_id: str
    how: str  # "volume", DUCK_MUTE or DUCK_PAUSE
    volume: float | None = None  # before
    target: float | None = None  # while ducked


@dataclass
class Session:
    """The players quieted while one satellite listens."""

    players: list[Ducked] = field(default_factory=list)
    ducking: asyncio.Task[None] | None = None
    restoring: asyncio.Task[None] | None = None
    cancel_timer: CALLBACK_TYPE | None = None


class MediaDucker:
    """Quiets the media players of the area of each listening satellite (see the module doc)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self._sessions: dict[str, Session] = {}
        self._held: set[str] = set()  # players some session quieted
        self._unsub: CALLBACK_TYPE | None = None

    def start(self) -> None:
        self._unsub = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._changed, event_filter=self._is_satellite
        )

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await self.release()

    async def release(self, satellite_id: str | None = None) -> None:
        """Give the players their sound back (of one satellite, or of all) and wait for it."""
        ids = [satellite_id] if satellite_id else list(self._sessions)
        tasks = [self._restore(s) for s in ids if s in self._sessions]
        if tasks:
            await asyncio.gather(*tasks)

    @callback
    def _is_satellite(self, data: EventStateChangedData) -> bool:
        return data["entity_id"].startswith(f"{SATELLITE_DOMAIN}.")

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        satellite_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        was = old is not None and old.state == LISTENING
        now = new is not None and new.state == LISTENING
        if now and not was:
            self._start(satellite_id)
        elif was and not now and satellite_id in self._sessions:
            self.entry.async_create_background_task(
                self.hass, self._restore(satellite_id), f"flowpilot restore media {satellite_id}"
            )

    def _start(self, satellite_id: str) -> None:
        if not self.entry.options.get(CONF_DUCK):
            return
        session = self._sessions.get(satellite_id)
        if session is not None and session.restoring is None:
            return  # still quiet from this command
        session = Session()
        self._sessions[satellite_id] = session
        session.ducking = self.entry.async_create_background_task(
            self.hass, self._duck(satellite_id, session), f"flowpilot duck media {satellite_id}"
        )

        @callback
        def stuck(_now: Any) -> None:
            if self._sessions.get(satellite_id) is session:
                self.entry.async_create_background_task(
                    self.hass, self._restore(satellite_id), "flowpilot restore media (timeout)"
                )

        session.cancel_timer = async_call_later(self.hass, MAX_DUCK_S, stuck)

    def _players(self, satellite_id: str) -> list[str]:
        """The sounding media players of the satellite's area, except its own and excluded ones."""
        entities = er.async_get(self.hass)
        satellite = entities.async_get(satellite_id)
        area_id = area_of(self.hass, satellite_id, None)
        if area_id is None:
            return []
        own_device = satellite.device_id if satellite else None
        excluded = set(self.entry.options.get(CONF_DUCK_EXCLUDE) or [])
        found = []
        for state in self.hass.states.async_all(MEDIA_PLAYER):
            entity_id = state.entity_id
            if state.state not in SOUNDING or entity_id in excluded or entity_id in self._held:
                continue
            entry = entities.async_get(entity_id)
            if entry is None or entity_area(self.hass, entry) != area_id:
                continue
            if own_device and entry.device_id == own_device:
                continue
            found.append(entity_id)
        return found

    async def _duck(self, satellite_id: str, session: Session) -> None:
        level = float(self.entry.options.get(CONF_DUCK_LEVEL) or DUCK_LEVEL_DEFAULT) / 100
        fallback = self.entry.options.get(CONF_DUCK_FALLBACK, DUCK_MUTE)
        plan = []
        for entity_id in self._players(satellite_id):
            if (ducked := self._plan(entity_id, level, fallback)) is not None:
                self._held.add(entity_id)
                session.players.append(ducked)
                plan.append(ducked)
        await asyncio.gather(*(self._quiet(d) for d in plan))
        if plan:
            _LOGGER.debug("Ducked for %s: %s", satellite_id, [d.entity_id for d in plan])

    def _plan(self, entity_id: str, level: float, fallback: str) -> Ducked | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0)
        volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        if state.attributes.get(ATTR_MEDIA_VOLUME_MUTED) is True:
            return None  # silent already
        if features & MediaPlayerEntityFeature.VOLUME_SET and isinstance(volume, (int, float)):
            target = round(float(volume) * level, 2)
            return Ducked(entity_id, "volume", float(volume), target) if target < volume else None
        if fallback == DUCK_MUTE and features & MediaPlayerEntityFeature.VOLUME_MUTE:
            return Ducked(entity_id, DUCK_MUTE)
        if (
            fallback == DUCK_PAUSE
            and features & MediaPlayerEntityFeature.PAUSE
            and state.state == MediaPlayerState.PLAYING
        ):
            return Ducked(entity_id, DUCK_PAUSE)
        return None

    async def _quiet(self, ducked: Ducked) -> None:
        if ducked.how == "volume":
            await self._call(SERVICE_VOLUME_SET, ducked.entity_id, volume_level=ducked.target)
        elif ducked.how == DUCK_MUTE:
            await self._call(SERVICE_VOLUME_MUTE, ducked.entity_id, is_volume_muted=True)
        else:
            await self._call(SERVICE_MEDIA_PAUSE, ducked.entity_id)

    async def _restore(self, satellite_id: str) -> None:
        session = self._sessions.get(satellite_id)
        if session is None:
            return
        if session.restoring is None:
            session.restoring = asyncio.ensure_future(self._give_back(satellite_id, session))
        await asyncio.shield(session.restoring)

    async def _give_back(self, satellite_id: str, session: Session) -> None:
        try:
            if session.cancel_timer is not None:
                session.cancel_timer()
            if session.ducking is not None:
                await session.ducking  # the players it is quieting right now come back too
            await asyncio.gather(*(self._loud(d) for d in session.players))
            await self._settle(session.players)
        finally:
            for ducked in session.players:
                self._held.discard(ducked.entity_id)
            if self._sessions.get(satellite_id) is session:
                del self._sessions[satellite_id]

    async def _loud(self, ducked: Ducked) -> None:
        state = self.hass.states.get(ducked.entity_id)
        if state is None or state.state not in {*SOUNDING, MediaPlayerState.PAUSED}:
            return  # turned off meanwhile
        if ducked.how == "volume":
            volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            ours = (ducked.target, ducked.volume)  # the player may not have reported ours yet
            if not isinstance(volume, (int, float)) or not any(
                v is not None and abs(volume - v) <= VOLUME_TOLERANCE for v in ours
            ):
                return  # someone set another volume: keep it
            await self._call(SERVICE_VOLUME_SET, ducked.entity_id, volume_level=ducked.volume)
        elif ducked.how == DUCK_MUTE:
            await self._call(SERVICE_VOLUME_MUTE, ducked.entity_id, is_volume_muted=False)
        elif state.state == MediaPlayerState.PAUSED:
            await self._call(SERVICE_MEDIA_PLAY, ducked.entity_id)

    async def _settle(self, players: list[Ducked]) -> None:
        """Wait a little for the players to report their volume back, so a command that counts
        from it ("louder") sees the real one."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_TIMEOUT_S
        while loop.time() < deadline:
            if all(self._settled(d) for d in players):
                return
            await asyncio.sleep(SETTLE_STEP_S)

    def _settled(self, ducked: Ducked) -> bool:
        state = self.hass.states.get(ducked.entity_id)
        if state is None or ducked.how != "volume" or ducked.target is None:
            return True
        volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        return (
            not isinstance(volume, (int, float)) or abs(volume - ducked.target) > VOLUME_TOLERANCE
        )

    async def _call(self, service: str, entity_id: str, **data: Any) -> None:
        try:
            async with asyncio.timeout(CALL_TIMEOUT_S):
                await self.hass.services.async_call(
                    MEDIA_PLAYER, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
                )
        except Exception as err:  # noqa: BLE001 - a player that fails must not break the command
            _LOGGER.warning("Cannot %s %s: %s", service, entity_id, err)
