"""Host operations Home Assistant performs for the FlowPilot runtime (ADR-014)."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components import conversation, stt, tts
from homeassistant.components.conversation import async_converse
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .client import FlowPilotClient, HostHandler, HostRequest
from .const import (
    ATTACH_TIMEOUT_S,
    DEFAULT_AGENT_ID,
    DOMAIN,
    HOST_OP_CONVERSATION,
    HOST_OP_STT,
    HOST_OP_TTS,
    INNER_CONVERSATION_PREFIX,
)
from .wav import open_wav


class LoopError(Exception):
    """The graph asked to run a FlowPilot entity from inside FlowPilot."""


class HostOpError(Exception):
    """A host operation cannot be performed with the given arguments."""


def build_host_ops(
    hass: HomeAssistant, entry: ConfigEntry[FlowPilotClient]
) -> dict[str, HostHandler]:
    def refuse_own(entity_id: str) -> None:
        entity = er.async_get(hass).async_get(entity_id)
        if entity is not None and entity.platform == DOMAIN:
            raise LoopError(f"{entity_id} is a FlowPilot entity; the graph must use a real one")

    async def conversation_process(request: HostRequest) -> Any:
        args = request.args
        agent_id = args.get("agent_id") or DEFAULT_AGENT_ID
        refuse_own(agent_id)

        user_input: conversation.ConversationInput | None = None
        if request.handle is not None:
            try:
                # Voice turns: Home Assistant calls our conversation entity right after STT.
                async with asyncio.timeout(ATTACH_TIMEOUT_S):
                    user_input = await asyncio.shield(request.handle.attached)
            except TimeoutError:
                user_input = None

        outer_id = args.get("conversation_id") or (
            user_input.conversation_id if user_input else None
        )
        inner_id = f"{INNER_CONVERSATION_PREFIX}{outer_id}" if outer_id else None
        result = await async_converse(
            hass,
            text=str(args["text"]),
            conversation_id=inner_id,
            context=user_input.context if user_input else Context(),
            language=args.get("language") or (user_input.language if user_input else None),
            agent_id=agent_id,
            device_id=args.get("device_id") or (user_input.device_id if user_input else None),
            satellite_id=args.get("satellite_id")
            or (user_input.satellite_id if user_input else None),
        )
        speech = result.response.speech.get("plain", {}).get("speech", "")
        return {
            "speech": speech,
            "continue_conversation": result.continue_conversation,
            "conversation_id": result.conversation_id,
            "response_type": result.response.response_type.value,
        }

    async def stt_process(request: HostRequest) -> Any:
        """Run an STT entity on the audio the runtime streams in."""
        args = request.args
        entity_id = str(args["entity_id"])
        refuse_own(entity_id)
        engine = stt.async_get_speech_to_text_entity(hass, entity_id)
        if engine is None:
            raise HostOpError(f"no speech-to-text entity {entity_id}")
        fmt = args["audio"]["format"]
        metadata = stt.SpeechMetadata(
            language=str(args.get("language") or hass.config.language),
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates(int(fmt["sample_rate"])),
            channel=stt.AudioChannels(int(fmt.get("channels", 1))),
        )
        if not engine.check_metadata(metadata):
            raise HostOpError(f"{entity_id} does not support {metadata}")
        result = await engine.internal_async_process_audio_stream(metadata, request.audio())
        return {"text": result.text or "", "state": result.result.value}

    async def tts_synthesize(request: HostRequest) -> Any:
        """Synthesize text with a TTS entity and stream the PCM back to the runtime."""
        args = request.args
        engine = str(args["engine"])
        options: dict[str, Any] = {
            tts.ATTR_PREFERRED_FORMAT: "wav",
            tts.ATTR_PREFERRED_SAMPLE_RATE: int(args.get("sample_rate") or 16000),
            tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
            tts.ATTR_PREFERRED_SAMPLE_BYTES: 2,
        }
        if args.get("voice"):
            options[tts.ATTR_VOICE] = args["voice"]
        stream = tts.async_create_stream(hass, engine, args.get("language"), options)
        stream.async_set_message(str(args["text"]))
        fmt, pcm = await open_wav(stream.async_stream_result())
        await request.send_audio(fmt.as_meta(), pcm)
        return {"sample_rate": fmt.sample_rate}

    return {
        HOST_OP_CONVERSATION: conversation_process,
        HOST_OP_STT: stt_process,
        HOST_OP_TTS: tts_synthesize,
    }
