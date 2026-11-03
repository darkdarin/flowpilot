"""Conversation shim: returns the FlowPilot answer of a turn, streaming it into the chat log.

Voice turns were started by our STT entity in the same pipeline run (ADR-012); the graph is
already running and this entity only collects its answer. Text turns (chat,
``conversation.process``) start a new FlowPilot turn here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import FlowPilotClient, FlowPilotError, TurnHandle
from .const import RESPONSE_TIMEOUT_S, SIGNAL_CONNECTION
from .correlation import take_voice_turn
from .entity import service_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[FlowPilotClient],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FlowPilotConversationEntity(entry)])


class FlowPilotConversationEntity(conversation.ConversationEntity):
    """Conversation agent backed by a FlowPilot graph."""

    _attr_has_entity_name = True
    _attr_name = "Agent"
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry[FlowPilotClient]) -> None:
        self._entry = entry
        self._client = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}-conversation"
        self._attr_device_info = service_device(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
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
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        # Home Assistant creates the dialogue id inside async_process; user_input may lack it.
        conversation_id = chat_log.conversation_id
        handle = take_voice_turn(conversation_id)
        if handle is None:
            try:
                handle = await self._client.start_turn(
                    source={
                        "kind": "ha.conversation",
                        "device_id": user_input.device_id,
                        "satellite_id": user_input.satellite_id,
                    },
                    language=user_input.language,
                    conversation_id=conversation_id,
                    utterance=user_input.text,
                    user_id=user_input.context.user_id,
                )
            except FlowPilotError as err:
                return self._error(user_input, chat_log, f"FlowPilot is unavailable: {err}")
            _LOGGER.debug(
                "Text turn %s (session %s) for %s",
                handle.turn_id,
                handle.session_id,
                conversation_id,
            )
        else:
            _LOGGER.debug(
                "Voice turn %s (session %s) continued for %s",
                handle.turn_id,
                handle.session_id,
                handle.conversation_id,
            )
        handle.attach(user_input)

        state: dict[str, object] = {"continue": False, "error": None}
        async for _ in chat_log.async_add_delta_content_stream(
            self.entity_id, self._deltas(handle, state)
        ):
            pass

        if state["error"] is not None and not chat_log.content[-1].role == "assistant":
            return self._error(user_input, chat_log, str(state["error"]))
        result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        result.continue_conversation = bool(state["continue"]) or result.continue_conversation
        return result

    @staticmethod
    async def _deltas(
        handle: TurnHandle, state: dict[str, object]
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Translate the turn's events into chat log deltas until the response is complete."""
        yield {"role": "assistant"}
        streamed = False
        while True:
            try:
                msg = await handle.next_event(RESPONSE_TIMEOUT_S)
            except TimeoutError:
                state["error"] = "FlowPilot did not answer in time"
                await handle.cancel()
                return
            kind = msg["type"]
            if kind == "turn.delta":
                streamed = True
                yield {"content": str(msg["text"])}
            elif kind == "turn.response":
                response = msg["response"]
                state["continue"] = bool(response.get("continue_conversation"))
                if not streamed:
                    yield {"content": str(response.get("text") or "")}
                return
            elif kind == "turn.end":
                if not streamed:
                    state["error"] = f"FlowPilot turn ended without an answer ({msg['status']})"
                return

    def _error(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
        message: str,
    ) -> conversation.ConversationResult:
        _LOGGER.warning(message)
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )
