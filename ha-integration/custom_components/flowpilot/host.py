"""Host operations Home Assistant performs for the FlowPilot runtime (ADR-014)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .client import FlowPilotClient, HostHandler, TurnHandle
from .const import (
    ATTACH_TIMEOUT_S,
    DEFAULT_AGENT_ID,
    DOMAIN,
    HOST_OP_CONVERSATION,
    INNER_CONVERSATION_PREFIX,
)


class LoopError(Exception):
    """The graph asked to run a FlowPilot agent from inside FlowPilot."""


def build_host_ops(
    hass: HomeAssistant, entry: ConfigEntry[FlowPilotClient]
) -> dict[str, HostHandler]:
    async def conversation_process(handle: TurnHandle | None, args: Mapping[str, Any]) -> Any:
        agent_id = args.get("agent_id") or DEFAULT_AGENT_ID
        entity = er.async_get(hass).async_get(agent_id)
        if entity is not None and entity.platform == DOMAIN:
            raise LoopError(f"{agent_id} is a FlowPilot agent; the graph must call a real agent")

        user_input: conversation.ConversationInput | None = None
        if handle is not None:
            try:
                # Voice turns: Home Assistant calls our conversation entity right after STT.
                async with asyncio.timeout(ATTACH_TIMEOUT_S):
                    user_input = await asyncio.shield(handle.attached)
            except TimeoutError:
                user_input = None

        outer_id = args.get("conversation_id") or (
            user_input.conversation_id if user_input else None
        )
        inner_id = f"{INNER_CONVERSATION_PREFIX}{outer_id}" if outer_id else None
        result = await conversation.async_converse(
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

    return {HOST_OP_CONVERSATION: conversation_process}
