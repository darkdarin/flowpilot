"""Constants for the FlowPilot integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "flowpilot"
CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"

PROTOCOL_VERSION: Final = 1
WS_PATH: Final = "/api/ws"

HOST_OP_CONVERSATION: Final = "ha.conversation.process"
HOST_OP_STT: Final = "ha.stt.process"
HOST_OP_TTS: Final = "ha.tts.synthesize"
DEFAULT_AGENT_ID: Final = "conversation.home_assistant"
# Conversation ids that are not ULIDs are kept as-is by Home Assistant, so the inner agent gets a
# stable dialogue of its own next to the FlowPilot one.
INNER_CONVERSATION_PREFIX: Final = "flowpilot:"

CONNECT_TIMEOUT_S: Final = 10.0
START_TIMEOUT_S: Final = 5.0
# How long a voice turn's host call waits for HA to call the conversation entity of the same run.
ATTACH_TIMEOUT_S: Final = 5.0
# How long the STT entity waits for the final transcript after the audio stream ended.
TRANSCRIPT_TIMEOUT_S: Final = 15.0
# How long the conversation entity waits for the response after the transcript.
RESPONSE_TIMEOUT_S: Final = 30.0

SIGNAL_CONNECTION: Final = "flowpilot_connection_{entry_id}"

STT_LANGUAGES: Final = [
    "ar", "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fa", "fi", "fr", "he", "hi",
    "hr", "hu", "id", "it", "ja", "ko", "lt", "lv", "nb", "nl", "pl", "pt", "ro", "ru", "sk",
    "sl", "sr", "sv", "th", "tr", "uk", "vi", "zh",
]  # fmt: skip
