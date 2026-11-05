"""FlowPilot plugin SDK: message contracts and node interfaces (contracts v1, ADR-013)."""

from .context import DeviceInfo, SessionInfo, SessionState, SourceInfo, TurnContext
from .host import HostAudio, HostError, HostUnavailable
from .ids import new_ulid
from .node import OPTIONS_KEY, Node, NodeContext, NodeSpec, PortSpec, TurnSession
from .payloads import (
    IDENTITY_PROVIDER_GRANT,
    Assurance,
    AudioChunk,
    AudioFormat,
    Evidence,
    Principal,
    Response,
    SpeakerMatch,
    SpeakerStatus,
    TextDelta,
    Transcript,
    Utterance,
    Word,
)
from .streams import Envelope, InputStream, OutputStream, StreamError, StreamHeader
from .text import FieldText, NodeText, node_translations
from .types import (
    PRIVILEGED_TYPES,
    payload_class,
    payload_type,
    registered_types,
    type_id_of,
    validate_type_id,
)

__version__ = "0.0.1"

__all__ = [
    "FieldText",
    "NodeText",
    "node_translations",
    "IDENTITY_PROVIDER_GRANT",
    "OPTIONS_KEY",
    "PRIVILEGED_TYPES",
    "Assurance",
    "AudioChunk",
    "AudioFormat",
    "DeviceInfo",
    "Envelope",
    "Evidence",
    "HostAudio",
    "HostError",
    "HostUnavailable",
    "InputStream",
    "Node",
    "NodeContext",
    "NodeSpec",
    "OutputStream",
    "PortSpec",
    "Principal",
    "Response",
    "SessionInfo",
    "SessionState",
    "SourceInfo",
    "SpeakerMatch",
    "SpeakerStatus",
    "StreamError",
    "StreamHeader",
    "TextDelta",
    "Transcript",
    "TurnContext",
    "TurnSession",
    "Utterance",
    "Word",
    "new_ulid",
    "payload_class",
    "payload_type",
    "registered_types",
    "type_id_of",
    "validate_type_id",
]
