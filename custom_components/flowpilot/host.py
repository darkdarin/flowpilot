"""Host operations Home Assistant performs for the FlowPilot runtime (ADR-014)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probatio
from homeassistant.components import conversation, stt, tts
from homeassistant.components.conversation import async_converse
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import llm
from homeassistant.helpers.json import JSONEncoder

from . import music
from .client import FlowPilotClient, HostHandler, HostRequest
from .const import (
    ATTACH_TIMEOUT_S,
    DEFAULT_AGENT_ID,
    DOMAIN,
    HOST_OP_CONVERSATION,
    HOST_OP_ENTITIES,
    HOST_OP_LLM_CALL,
    HOST_OP_LLM_TOOLS,
    HOST_OP_NAMES,
    HOST_OP_PEOPLE,
    HOST_OP_STT,
    HOST_OP_TTS,
    INNER_CONVERSATION_PREFIX,
    OPTION_CONVERSATION,
    OPTION_STT,
    OPTION_TTS,
    OPTION_USER,
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
            # why an "error" answer failed: "no_intent_match" (not understood),
            # "no_valid_targets", "failed_to_handle", ...
            "error_code": (
                result.response.error_code.value if result.response.error_code else None
            ),
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

    async def entities_list(request: HostRequest) -> Any:
        """What graphs may refer to: STT/TTS/conversation entities, Wyoming services, users."""
        registry = er.async_get(hass)

        def foreign(entity_id: str) -> bool:
            entity = registry.async_get(entity_id)
            return entity is None or entity.platform != DOMAIN

        def states(domain: str) -> list[dict[str, str]]:
            return [
                {"id": state.entity_id, "name": state.name}
                for state in hass.states.async_all(domain)
                if foreign(state.entity_id)
            ]

        wyoming: dict[str, list[dict[str, str]]] = {"wyoming.asr": [], "wyoming.tts": []}
        for wy in hass.config_entries.async_entries("wyoming", include_disabled=False):
            host, port = wy.data.get(CONF_HOST), wy.data.get(CONF_PORT)
            if not host or not port:
                continue
            domains = {e.domain for e in er.async_entries_for_config_entry(registry, wy.entry_id)}
            option = {"id": f"tcp://{host}:{port}", "name": wy.title}
            if "stt" in domains:
                wyoming["wyoming.asr"].append(option)
            if "tts" in domains:
                wyoming["wyoming.tts"].append(option)

        people = [
            {"id": user.id, "name": user.name or user.id}
            for user in await hass.auth.async_get_users()
            if user.is_active and not user.system_generated
        ]
        return {
            "options": {
                OPTION_USER: people,
                OPTION_STT: states("stt"),
                OPTION_TTS: states("tts"),
                OPTION_CONVERSATION: states("conversation"),
                **wyoming,
            },
            # Entities outside these lists cannot work; Wyoming services are only suggestions,
            # FlowPilot connects to them directly.
            "strict": [OPTION_STT, OPTION_TTS, OPTION_CONVERSATION],
        }

    async def turn_input(request: HostRequest) -> conversation.ConversationInput | None:
        """The Assist request behind the turn, when Home Assistant started it."""
        if request.handle is None:
            return None
        try:
            async with asyncio.timeout(ATTACH_TIMEOUT_S):
                return await asyncio.shield(request.handle.attached)
        except TimeoutError:
            return None

    async def llm_api(request: HostRequest) -> llm.APIInstance:
        """Home Assistant's Assist LLM API for the turn's person, device and language: only
        entities exposed to Assist, tools run with the person's context."""
        args = request.args
        user_input = await turn_input(request)
        user_id = args.get("user_id")
        context = (
            user_input.context
            if user_input is not None
            else Context(user_id=str(user_id) if user_id else None)
        )
        llm_context = llm.LLMContext(
            platform=DOMAIN,
            context=context,
            language=args.get("language") or (user_input.language if user_input else None),
            assistant=conversation.DOMAIN,
            device_id=args.get("device_id") or (user_input.device_id if user_input else None),
        )
        return await llm.async_get_api(hass, llm.LLM_API_ASSIST, llm_context)

    async def llm_tools(request: HostRequest) -> Any:
        """The Assist API's instructions and tools, with JSON Schema parameters."""
        api = await llm_api(request)
        tools = []
        for tool in api.tools:
            schema = probatio.to_openapi(
                tool.parameters, custom_serializer=api.custom_serializer, openapi_version="3.1.0"
            )
            # like Home Assistant's OpenAI integration: providers reject these at the top level
            schema = {
                k: v
                for k, v in schema.items()
                if k not in {"oneOf", "anyOf", "allOf", "enum", "not"}
            }
            tools.append(
                {"name": tool.name, "description": tool.description or "", "parameters": schema}
            )
        # FlowPilot's own tools where Home Assistant's fall short (starting music)
        tools.extend(music.music_tools(hass))
        return {"prompt": api.api_prompt, "tools": tools}

    async def llm_call(request: HostRequest) -> Any:
        """Run one tool of the Assist API; failures come back as text for the model."""
        api = await llm_api(request)
        arguments = request.args.get("arguments") or {}
        name = str(request.args["name"])
        if name == music.TOOL_NAME:
            try:
                found = await music.play_music(hass, api.llm_context, dict(arguments))
            except (HomeAssistantError, ValueError) as err:
                return {"error": True, "data": {"error": type(err).__name__, "message": str(err)}}
            return {
                "error": "error" in found,
                "data": json.loads(json.dumps(found, cls=JSONEncoder)),
            }
        tool_input = llm.ToolInput(tool_name=name, tool_args=dict(arguments))
        try:
            result = await api.async_call_tool(tool_input)
        except (HomeAssistantError, ValueError) as err:  # bad arguments, unknown entity, ...
            return {"error": True, "data": {"error": type(err).__name__, "message": str(err)}}
        data = json.loads(json.dumps(result.data, cls=JSONEncoder))
        return {"error": result.error, "data": data}

    async def names_list(request: HostRequest) -> Any:
        """Every name Assist matches: areas, floors and entities exposed to it, with aliases."""
        registry = er.async_get(hass)
        names: set[str] = set()
        for area in ar.async_get(hass).async_list_areas():
            names.update([area.name, *area.aliases])
        for floor in fr.async_get(hass).async_list_floors():
            names.update([floor.name, *floor.aliases])
        for state in hass.states.async_all():
            if not async_should_expose(hass, conversation.DOMAIN, state.entity_id):
                continue
            names.add(state.name)
            entry = registry.async_get(state.entity_id)
            if entry is not None:
                names.update(a for a in entry.aliases if isinstance(a, str))
        return {"names": sorted(n for n in names if n and n.strip())}

    async def people_list(request: HostRequest) -> Any:
        """Who lives in the home: Home Assistant people with where they are, and users without a
        person, so an agent knows who it talks to and who is home."""
        people: list[dict[str, Any]] = []
        users_with_person: set[str] = set()
        for state in hass.states.async_all("person"):
            user_id = state.attributes.get("user_id")
            if isinstance(user_id, str):
                users_with_person.add(user_id)
            people.append(
                {
                    "name": state.name,
                    "user_id": user_id if isinstance(user_id, str) else None,
                    "entity_id": state.entity_id,
                    # "home", "not_home", a zone's name, or "unknown"
                    "state": state.state,
                }
            )
        for user in await hass.auth.async_get_users():
            if user.is_active and not user.system_generated and user.id not in users_with_person:
                people.append(
                    {
                        "name": user.name or user.id,
                        "user_id": user.id,
                        "entity_id": None,
                        "state": None,
                    }
                )
        return {"people": people}

    return {
        HOST_OP_ENTITIES: entities_list,
        HOST_OP_PEOPLE: people_list,
        HOST_OP_NAMES: names_list,
        HOST_OP_LLM_TOOLS: llm_tools,
        HOST_OP_LLM_CALL: llm_call,
        HOST_OP_CONVERSATION: conversation_process,
        HOST_OP_STT: stt_process,
        HOST_OP_TTS: tts_synthesize,
    }
