"""The FlowPilot text-to-speech entity in a real Assist pipeline (ADR-017).

Regression tests for the Home Assistant details the entity relies on: text-to-speech is
synthesized in a task that inherits the pipeline run's context (our conversation entity's
``CURRENT_SPEECH``), the plain ``default_options`` property is read for every stream, the
graph version in the options takes part in the cache key, and Assist's own acknowledge sound is
at ``assist_pipeline.const.ACKNOWLEDGE_PATH``. If any of them changes, these fail.
"""

from __future__ import annotations

import io
import uuid
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import (
    PipelineEventType,
    async_pipeline_from_audio_stream,
)
from homeassistant.components.assist_pipeline.models import PipelineStage
from homeassistant.components.assist_pipeline.pipeline import PipelineData
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.flowpilot.tts import VERSION_OPTION, FlowPilotTtsEntity

from .conftest import PHRASE, Runtime, _runtime, setup_flowpilot
from .test_integration import event, silence

pytestmark = pytest.mark.usefixtures("socket_enabled")

TTS_ENTITY = "tts.flowpilot_text_to_speech"
LONG = " and that is all there is to it, said FlowPilot to the satellite"  # > 60 characters


def speaking_graph(answer: str) -> dict[str, Any]:
    """Speech in, a fixed answer, spoken by the beeping text-to-speech: 10 ms per character."""
    return {
        "schema_version": 1,
        "id": "test",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "stt", "type": "dev.fixed_stt", "config": {"text": PHRASE, "max_chunks": 10}},
            {"id": "agent", "type": "debug.answer", "config": {"text": answer}},
            {"id": "voice", "type": "dev.beep_tts"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "in.audio", "to": "stt.audio"},
            {"from": "stt.transcript", "to": "out.transcript"},
            {"from": "stt.utterance", "to": "agent.utterance"},
            {"from": "agent.delta", "to": "out.delta"},
            {"from": "agent.delta", "to": "voice.text"},
            {"from": "agent.response", "to": "out.response"},
            {"from": "voice.speech", "to": "out.speech"},
        ],
    }


@pytest.fixture(autouse=True)
def drop_cached_speech(hass: HomeAssistant) -> Iterator[None]:
    """Remove what these tests leave in Home Assistant's text-to-speech file cache."""
    yield
    for path in Path(hass.config.path("tts")).glob("*tts.flowpilot_*"):
        path.unlink(missing_ok=True)


@pytest.fixture
async def short_runtime() -> AsyncIterator[Runtime]:
    async for rt in _runtime({}, speaking_graph("echo: {text}")):
        yield rt


@pytest.fixture
async def long_runtime() -> AsyncIterator[Runtime]:
    async for rt in _runtime({}, speaking_graph("echo: {text}" + LONG)):
        yield rt


async def speak_turn(hass: HomeAssistant, output: str | None = "wav") -> tuple[str, bytes]:
    """One voice turn through STT, our agent and our text-to-speech; the answer and its audio."""
    data: PipelineData = hass.data["assist_pipeline"]
    pipeline = await data.pipeline_store.async_create_item(
        {
            "name": "FlowPilot speaks",
            "language": "en",
            "conversation_engine": "conversation.flowpilot_agent",
            "conversation_language": "en",
            "stt_engine": "stt.flowpilot_speech_to_text",
            "stt_language": "en",
            "tts_engine": TTS_ENTITY,
            "tts_language": "en",
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    events: list[Any] = []
    await async_pipeline_from_audio_stream(
        hass,
        context=Context(),
        event_callback=events.append,
        stt_metadata=stt.SpeechMetadata(
            language="en",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        ),
        stt_stream=silence(),
        pipeline_id=pipeline.id,
        start_stage=PipelineStage.STT,
        end_stage=PipelineStage.TTS,
        tts_audio_output=output,
    )
    answer = event(events, PipelineEventType.INTENT_END)["intent_output"]["response"]
    stream = tts.async_get_stream(
        hass, event(events, PipelineEventType.TTS_END)["tts_output"]["token"]
    )
    assert stream is not None
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    return str(answer["speech"]["plain"]["speech"]), audio


async def setup_speaking(hass: HomeAssistant, runtime: Runtime) -> MockConfigEntry:
    """The integration, with a graph version unique to this test.

    Home Assistant keeps synthesized audio in a file cache that outlives the test (and the test
    run); a fresh version keeps each test's cache keys its own, so the entity is really called.
    """
    entry = await setup_flowpilot(hass, runtime)
    report(entry, uuid.uuid4().hex)
    return entry


def report(entry: MockConfigEntry, version: str) -> None:
    """The runtime reports the graph with this version (as after a deploy)."""
    entry.runtime_data._dispatch(
        {"type": "graphs", "graphs": [{"id": "test", "version": version}], "default": "test"}
    )


def pcm_of(audio: bytes) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) == (16000, 2, 1)
        return wav.readframes(wav.getnframes() or 10**9)


def beeps(text: str) -> bytes:
    return (1000).to_bytes(2, "little", signed=True) * (160 * len(text))


async def test_plays_the_graph_speech(hass: HomeAssistant, short_runtime: Runtime) -> None:
    await setup_speaking(hass, short_runtime)
    answer, audio = await speak_turn(hass)
    assert answer == f"echo: {PHRASE}"
    assert pcm_of(audio) == beeps(answer)  # the graph's voice, not a Home Assistant one


async def test_plays_streamed_speech(hass: HomeAssistant, long_runtime: Runtime) -> None:
    """Long answers stream: synthesis starts while the agent is still answering."""
    await setup_speaking(hass, long_runtime)
    answer, audio = await speak_turn(hass)
    assert answer == f"echo: {PHRASE}{LONG}" and len(answer) > 60
    assert pcm_of(audio) == beeps(answer)


async def test_cache_follows_the_graph_version(hass: HomeAssistant, short_runtime: Runtime) -> None:
    entry = await setup_speaking(hass, short_runtime)
    entity = next(
        e
        for platform in entity_platform.async_get_platforms(hass, "flowpilot")
        for e in platform.entities.values()
        if e.entity_id == TTS_ENTITY
    )
    assert isinstance(entity, FlowPilotTtsEntity)

    v1, v2 = uuid.uuid4().hex, uuid.uuid4().hex
    report(entry, v1)
    assert entity.default_options == {VERSION_OPTION: v1}
    calls = 0
    original = FlowPilotTtsEntity.async_stream_tts_audio

    async def counted(self: FlowPilotTtsEntity, request: TTSAudioRequest) -> Any:
        nonlocal calls
        calls += 1
        return await original(self, request)

    with patch.object(FlowPilotTtsEntity, "async_stream_tts_audio", counted):
        await speak_turn(hass)
        await speak_turn(hass)
        assert calls == 1  # the same answer of the same graph comes from the cache
        report(entry, v2)  # a deploy changed the graph
        assert entity.default_options == {VERSION_OPTION: v2}  # read again, not cached
        answer, audio = await speak_turn(hass)
        assert calls == 2
        assert pcm_of(audio) == beeps(answer)


async def test_without_a_turn_uses_the_fallback_voice(
    hass: HomeAssistant, short_runtime: Runtime
) -> None:
    entry = await setup_flowpilot(hass, short_runtime)
    entity = FlowPilotTtsEntity(entry)
    entity.hass = hass

    async def text() -> AsyncGenerator[str]:
        yield "hello"

    with pytest.raises(HomeAssistantError, match="fallback voice"):
        await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))

    hass.config_entries.async_update_entry(entry, options={"fallback_tts": "tts.other"})

    class Voice:
        supported_languages = ["en-US"]

    class Stream:
        extension = "wav"
        message = ""

        def async_set_message(self, message: str) -> None:
            self.message = message

        async def async_stream_result(self) -> AsyncIterator[bytes]:
            yield b"other voice"

    stream = Stream()
    with (
        patch("custom_components.flowpilot.tts.get_engine_instance", return_value=Voice()),
        patch.object(tts, "async_create_stream", return_value=stream) as create,
    ):
        response = await entity.async_stream_tts_audio(TTSAudioRequest("en", {}, text()))
        assert [c async for c in response.data_gen] == [b"other voice"]
    assert stream.message == "hello"
    assert create.call_args.args[1:3] == ("tts.other", "en-US")


async def test_an_acknowledged_turn_plays_home_assistants_own_sound(hass: HomeAssistant) -> None:
    """Confirmed with a sound (Client output's ack), the turn has no speech: the entity plays the
    acknowledge sound of Assist, read from ``ACKNOWLEDGE_PATH`` (not a public API: if it moves,
    this fails)."""
    from homeassistant.components.assist_pipeline.const import ACKNOWLEDGE_PATH  # noqa: PLC0415

    acknowledged = speaking_graph("done")
    acknowledged["nodes"] = [n for n in acknowledged["nodes"] if n["id"] != "voice"]
    acknowledged["edges"] = [
        *(e for e in acknowledged["edges"] if "voice" not in e["from"] + e["to"]),
        {"from": "agent.response", "to": "out.ack"},
    ]
    async for runtime in _runtime({}, acknowledged):
        await setup_speaking(hass, runtime)
        answer, audio = await speak_turn(hass, output=None)
        assert answer == "done"
        assert audio == ACKNOWLEDGE_PATH.read_bytes()
