"""Text-to-speech shim: plays the speech the FlowPilot graph synthesized for the turn (ADR-017).

Pick a FlowPilot text-to-speech entity in an Assist assistant and the answer is spoken by the
graph's own text-to-speech nodes (any voice, a voice per speaker) instead of a Home Assistant voice.
The entity finds the turn through the pipeline run's context (``correlation.take_speech``), never by
the text. When there is no FlowPilot turn to play (a local intent answered first, a satellite
announcement, ``tts.speak``) or the graph produced no speech, the text is spoken by the fallback
voice chosen in the integration's options.

Home Assistant caches synthesized audio by text, language, options and engine. Each entity adds
the deployed graph's version to its options, so a deploy that changes the graph changes the cache
key, while repeated answers of an unchanged graph are served from the cache like any voice.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from homeassistant.components import tts
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import FlowPilotClient, TurnHandle
from .const import CONF_FALLBACK_TTS, DOMAIN, RESPONSE_TIMEOUT_S, STT_LANGUAGES
from .correlation import take_speech
from .entity import service_device
from .graphs import GraphEntity, async_track_graphs, graph_unique_id

_LOGGER = logging.getLogger(__name__)

# Not a voice setting: part of Home Assistant's cache key (see the module docstring).
VERSION_OPTION = "flowpilot_graph_version"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[FlowPilotClient],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FlowPilotTtsEntity(entry)])
    async_track_graphs(
        hass, entry, async_add_entities, "tts", lambda g, n: FlowPilotTtsEntity(entry, g, n)
    )


def wav_header(fmt: Mapping[str, Any]) -> bytes:
    """A WAV header for streaming: zero frames, as Home Assistant's Wyoming voices send it."""
    with io.BytesIO() as out:
        with wave.open(out, "wb") as wav:
            wav.setnchannels(int(fmt.get("channels", 1)))
            wav.setsampwidth(2)
            wav.setframerate(int(fmt["sample_rate"]))
        return out.getvalue()


class FlowPilotTtsEntity(GraphEntity, tts.TextToSpeechEntity):
    """Speaks the answer with the voice of a FlowPilot graph."""

    _attr_has_entity_name = True
    _domain = "tts"
    _base_name = "Text-to-speech"

    def __init__(
        self,
        entry: ConfigEntry[FlowPilotClient],
        graph: str | None = None,
        graph_name: str | None = None,
    ) -> None:
        self._entry = entry
        self._client = entry.runtime_data
        self.setup_graph(graph, graph_name)
        self._attr_unique_id = graph_unique_id(entry, "tts", graph)
        self._attr_device_info = service_device(entry)

    @property
    def supported_languages(self) -> list[str]:
        return STT_LANGUAGES

    @property
    def default_language(self) -> str:
        language = (self.hass.config.language or "en").split("-")[0].lower()
        return language if language in STT_LANGUAGES else "en"

    @property
    def supported_options(self) -> list[str]:
        return [VERSION_OPTION]

    @property
    def default_options(self) -> Mapping[str, Any] | None:
        # A plain property on purpose: Home Assistant caches the base class's, and the version
        # changes with every deploy (read when each text-to-speech stream is created).
        graph = self.graph or self._client.default_graph
        version = self._client.graph_versions.get(graph) if graph else None
        return {VERSION_OPTION: version} if version else None

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> tts.TTSAudioResponse:
        handle = take_speech()
        if handle is not None:
            if handle.graph != self.graph:
                _LOGGER.warning(
                    "The assistant pairs %s with the agent of graph %s; playing that graph's "
                    "speech. Pick the same graph for both",
                    self.entity_id,
                    handle.graph or "(default)",
                )
            try:
                async with asyncio.timeout(RESPONSE_TIMEOUT_S):
                    fmt = await handle.speech.format
            except TimeoutError:
                fmt = None
            if fmt is not None:
                return tts.TTSAudioResponse("wav", self._graph_speech(handle, fmt))
            _LOGGER.debug("Turn %s has no speech; using the fallback voice", handle.turn_id)
        text = "".join([chunk async for chunk in request.message_gen])
        return await self._fallback(text, request.language)

    @staticmethod
    async def _graph_speech(handle: TurnHandle, fmt: Mapping[str, Any]) -> AsyncGenerator[bytes]:
        yield wav_header(fmt)
        async for pcm in handle.speech.chunks():
            yield pcm

    async def _fallback(self, text: str, language: str) -> tts.TTSAudioResponse:
        """Speak ``text`` with the fallback voice from the integration's options."""
        engine = self._entry.options.get(CONF_FALLBACK_TTS)
        if not engine:
            raise HomeAssistantError(
                "FlowPilot has no speech of its own for this text; choose a fallback voice in "
                "the FlowPilot Assist integration options"
            )
        entity = er.async_get(self.hass).async_get(engine)
        if entity is not None and entity.platform == DOMAIN:
            raise HomeAssistantError(f"The fallback voice {engine} is FlowPilot itself")
        voice = get_engine_instance(self.hass, engine)
        if voice is None:
            raise HomeAssistantError(f"The fallback voice {engine} is not available")
        languages = voice.supported_languages or []
        base = language.split("-")[0].lower()
        match = next((x for x in languages if x.split("-")[0].split("_")[0].lower() == base), None)
        stream = tts.async_create_stream(
            self.hass,
            engine,
            language if language in languages else match,
            {tts.ATTR_PREFERRED_FORMAT: "wav"},
        )
        stream.async_set_message(text)
        return tts.TTSAudioResponse(stream.extension, stream.async_stream_result())
