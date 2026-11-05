"""Speech-to-text shim: hands the satellite audio to FlowPilot and returns its final transcript."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.components.stt.models import SpeechAudioProcessing
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import chat_session
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import FlowPilotClient, FlowPilotError, TurnHandle
from .const import SIGNAL_CONNECTION, STT_LANGUAGES, TRANSCRIPT_TIMEOUT_S
from .correlation import CURRENT_TURN
from .entity import service_device
from .graphs import GraphEntity, async_track_graphs, graph_unique_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[FlowPilotClient],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FlowPilotSpeechToText(entry)])
    async_track_graphs(
        hass, entry, async_add_entities, "stt", lambda g, n: FlowPilotSpeechToText(entry, g, n)
    )


class FlowPilotSpeechToText(GraphEntity, stt.SpeechToTextEntity):
    """Starts a FlowPilot voice turn; the graph decides how speech is recognized."""

    _attr_has_entity_name = True
    _domain = "stt"
    _base_name = "Speech-to-text"

    def __init__(
        self,
        entry: ConfigEntry[FlowPilotClient],
        graph: str | None = None,
        graph_name: str | None = None,
    ) -> None:
        self._entry = entry
        self._client = entry.runtime_data
        self.setup_graph(graph, graph_name)
        self._attr_unique_id = graph_unique_id(entry, "stt", graph)
        self._attr_device_info = service_device(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(entry_id=self._entry.entry_id),
                self._on_connection,
            )
        )

    @callback
    def _on_connection(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.connected

    @property
    def supported_languages(self) -> list[str]:
        return STT_LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        return [stt.AudioChannels.CHANNEL_MONO]

    @property
    def audio_processing(self) -> SpeechAudioProcessing:
        # The graph detects the end of speech itself and wants raw audio.
        return SpeechAudioProcessing(
            requires_external_vad=False,
            prefers_auto_gain_enabled=False,
            prefers_noise_reduction_enabled=False,
        )

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        session = chat_session.current_session.get()
        conversation_id = session.conversation_id if session else None
        try:
            handle = await self._client.start_turn(
                source={"kind": "ha.assist"},
                language=metadata.language,
                conversation_id=conversation_id,
                audio_format={
                    "encoding": "s16le",
                    "sample_rate": int(metadata.sample_rate),
                    "channels": int(metadata.channel),
                },
                graph=self.graph,  # a voice turn runs the graph its speech-to-text belongs to
            )
        except FlowPilotError as err:
            _LOGGER.error("Cannot start FlowPilot turn: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        # ADR-012: the conversation entity of this pipeline run picks the turn up from here.
        CURRENT_TURN.set(handle)

        feeder = asyncio.create_task(self._feed(handle, stream))
        try:
            text = await self._wait_final(handle, feeder)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError, FlowPilotError):
                await feeder
            with contextlib.suppress(FlowPilotError):
                await handle.end_audio()
        if text is None:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)

    @staticmethod
    async def _feed(handle: TurnHandle, stream: AsyncIterable[bytes]) -> None:
        async for chunk in stream:
            await handle.send_audio(chunk)
        await handle.end_audio()

    @staticmethod
    async def _wait_final(handle: TurnHandle, feeder: asyncio.Task[None]) -> str | None:
        """Wait for the final transcript; audio keeps flowing until it arrives."""
        while True:
            # No time limit while the satellite still sends audio; then a bounded wait.
            timeout = None if not feeder.done() else TRANSCRIPT_TIMEOUT_S
            get = asyncio.ensure_future(handle.events.get())
            done, _ = await asyncio.wait(
                {get, feeder} if not feeder.done() else {get},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get not in done:
                get.cancel()
                if feeder in done:
                    continue  # audio ended: loop again with the bounded wait
                return None  # timed out
            msg = get.result()
            if msg["type"] == "turn.transcript" and msg.get("final"):
                return str(msg["text"])
            if msg["type"] == "turn.end":
                handle.events.put_nowait(msg)  # let the conversation entity see the end too
                return None
