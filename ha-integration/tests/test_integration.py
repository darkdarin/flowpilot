"""The FlowPilot integration inside a real Home Assistant, against a real FlowPilot runtime.

The voice test is the regression test for ADR-012: the STT and conversation stages of one Assist
pipeline run must be recognized as the same FlowPilot turn. If Home Assistant ever runs the stages
in different asyncio contexts, the conversation entity starts a second (text) turn and this test
fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from custom_components.flowpilot.client import HostRequest
from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    async_pipeline_from_audio_stream,
)
from homeassistant.components.assist_pipeline.models import PipelineStage
from homeassistant.components.assist_pipeline.pipeline import PipelineData
from homeassistant.components.conversation import ConversationResult, async_converse
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import PHRASE, Runtime, setup_flowpilot

STT_ENTITY = "stt.flowpilot_speech_to_text"
AGENT_ENTITY = "conversation.flowpilot_agent"

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def silence(frames: int = 100) -> AsyncIterator[bytes]:
    for _ in range(frames):
        yield bytes(640)


async def run_voice_turn(hass: HomeAssistant) -> list[PipelineEvent]:
    data: PipelineData = hass.data["assist_pipeline"]
    pipeline = await data.pipeline_store.async_create_item(
        {
            "name": "FlowPilot test",
            "language": "en",
            "conversation_engine": AGENT_ENTITY,
            "conversation_language": "en",
            "stt_engine": STT_ENTITY,
            "stt_language": "en",
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    )
    events: list[PipelineEvent] = []
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
        end_stage=PipelineStage.INTENT,
    )
    return events


def event(events: list[PipelineEvent], kind: PipelineEventType) -> dict[str, Any]:
    found = [e for e in events if e.type == kind]
    assert found, f"no {kind} in {[e.type for e in events]}"
    data = found[0].data
    assert data is not None
    return data


async def test_voice_turn_is_one_flowpilot_turn(hass: HomeAssistant, echo_runtime: Runtime) -> None:
    await setup_flowpilot(hass, echo_runtime)
    events = await run_voice_turn(hass)

    assert event(events, PipelineEventType.STT_END)["stt_output"]["text"] == PHRASE
    speech = event(events, PipelineEventType.INTENT_END)["intent_output"]["response"]["speech"]
    assert speech["plain"]["speech"] == f"echo: {PHRASE}"
    # ADR-012: the conversation entity continued the STT entity's turn instead of starting one.
    assert echo_runtime.turns() == 1


async def test_text_turn(hass: HomeAssistant, echo_runtime: Runtime) -> None:
    await setup_flowpilot(hass, echo_runtime)
    first = await async_converse(hass, "hello", None, Context(), agent_id=AGENT_ENTITY)
    second = await async_converse(
        hass, "again", first.conversation_id, Context(), agent_id=AGENT_ENTITY
    )
    assert first.response.speech["plain"]["speech"] == "echo: hello"
    assert second.response.speech["plain"]["speech"] == "echo: again"
    assert first.conversation_id == second.conversation_id
    assert echo_runtime.turns() == 2


async def test_host_operation_runs_real_agent_in_own_dialogue(
    hass: HomeAssistant, ha_agent_runtime: Runtime
) -> None:
    await setup_flowpilot(hass, ha_agent_runtime)
    reply = intent.IntentResponse(language="en")
    reply.async_set_speech("It is noon")
    inner = AsyncMock(
        return_value=ConversationResult(response=reply, conversation_id="flowpilot:x")
    )
    with patch("custom_components.flowpilot.host.async_converse", inner):
        result = await async_converse(hass, "what time", None, Context(), agent_id=AGENT_ENTITY)

    assert result.response.speech["plain"]["speech"] == "It is noon"
    kwargs = inner.call_args.kwargs
    assert kwargs["agent_id"] == "conversation.home_assistant"
    assert kwargs["text"] == "what time"
    assert kwargs["conversation_id"] == f"flowpilot:{result.conversation_id}"


async def test_host_operation_refuses_flowpilot_agent(
    hass: HomeAssistant, ha_agent_runtime: Runtime
) -> None:
    entry = await setup_flowpilot(hass, ha_agent_runtime)
    handler = entry.runtime_data.host_ops["ha.conversation.process"]
    with pytest.raises(Exception, match="FlowPilot entity"):
        await handler(HostRequest(None, {"text": "x", "agent_id": AGENT_ENTITY}))


async def test_runtime_unavailable_gives_error_response(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain="flowpilot", data={"url": "http://127.0.0.1:9", "token": "x"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch("custom_components.flowpilot.client.START_TIMEOUT_S", 0.2):
        result = await async_converse(hass, "hi", None, Context(), agent_id=AGENT_ENTITY)
    assert result.response.response_type == intent.IntentResponseType.ERROR
    await hass.config_entries.async_unload(entry.entry_id)


async def test_ha_stt_and_tts_through_the_client(
    hass: HomeAssistant, speech_runtime: Runtime
) -> None:
    """Audio flows runtime -> HA STT entity and HA TTS -> runtime over host operations."""
    from .test_host_ops import FakeSttEntity, make_wav, pieces

    await setup_flowpilot(hass, speech_runtime)
    engine = FakeSttEntity()
    tts_stream = MagicMock()
    pcm = bytes(range(100)) * 8

    async def tts_result() -> AsyncIterator[bytes]:
        async for piece in pieces(make_wav(pcm, rate=16000, streaming=True), size=100):
            yield piece

    tts_stream.async_stream_result = tts_result
    original = stt.async_get_speech_to_text_entity

    def get_stt(hass_: HomeAssistant, entity_id: str) -> Any:
        return engine if entity_id == "stt.fake" else original(hass_, entity_id)

    with (
        patch.object(stt, "async_get_speech_to_text_entity", side_effect=get_stt),
        patch.object(tts, "async_create_stream", return_value=tts_stream),
    ):
        events = await run_voice_turn(hass)

    assert event(events, PipelineEventType.STT_END)["stt_output"]["text"] == "turn on the light"
    assert engine.received == 100 * 640  # all pipeline audio reached the HA STT entity
    trace = speech_runtime.trace
    # The pipeline ends at intent; the runtime finishes synthesizing on its own.
    for _ in range(100):
        if trace.named("turn.end"):
            break
        await asyncio.sleep(0.02)
    tts_stream.async_set_message.assert_called_once_with("echo: turn on the light")
    assert [e.attrs["status"] for e in trace.named("node.end", "tts")] == ["ok"]
    assert any(e.attrs["port"] == "speech" for e in trace.named("port.first_item", "tts"))
