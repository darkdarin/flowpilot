"""Built-in payload types (contracts v1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from .types import payload_type

IDENTITY_PROVIDER_GRANT = "identity.provider"


# --- audio ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Format of an audio stream; carried in the stream header, not in every chunk."""

    sample_rate: int
    channels: int = 1
    encoding: str = "s16le"

    @property
    def bytes_per_sample(self) -> int:
        if self.encoding != "s16le":
            raise ValueError(f"unsupported encoding {self.encoding!r}")
        return 2 * self.channels

    def as_meta(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }

    @classmethod
    def from_meta(cls, meta: Mapping[str, Any]) -> AudioFormat:
        return cls(
            sample_rate=int(meta["sample_rate"]),
            channels=int(meta.get("channels", 1)),
            encoding=str(meta.get("encoding", "s16le")),
        )


@payload_type("audio.pcm/1")
@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Raw PCM audio. ``sample_offset`` is the position of the first sample since stream start."""

    TYPE_ID: ClassVar[str]

    pcm: bytes
    sample_offset: int


# --- text ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int


@payload_type("text.transcript/1")
@dataclass(frozen=True, slots=True)
class Transcript:
    """Speech recognition result; ``final=False`` for partial hypotheses."""

    TYPE_ID: ClassVar[str]

    text: str
    final: bool
    language: str | None = None
    words: tuple[Word, ...] | None = None
    confidence: float | None = None


@payload_type("text.utterance/1", kind="value")
@dataclass(frozen=True, slots=True)
class Utterance:
    """The finished text of a user turn, from speech recognition or a text input."""

    TYPE_ID: ClassVar[str]

    text: str
    language: str | None = None


@payload_type("text.delta/1")
@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of streamed response text."""

    TYPE_ID: ClassVar[str]

    text: str


# --- identity ------------------------------------------------------------------------------------


class SpeakerStatus(StrEnum):
    MATCHED = "matched"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    ERROR = "error"


@payload_type("identity.speaker/1", kind="value")
@dataclass(frozen=True, slots=True)
class SpeakerMatch:
    """Raw speaker identification result. Not an authorization source by itself."""

    TYPE_ID: ClassVar[str]

    status: SpeakerStatus
    speaker_id: str | None = None
    score: float | None = None
    threshold: float | None = None
    model_id: str | None = None
    reason: str | None = None

    @classmethod
    def unknown(cls, reason: str, *, model_id: str | None = None) -> SpeakerMatch:
        return cls(status=SpeakerStatus.UNKNOWN, reason=reason, model_id=model_id)


class Assurance(StrEnum):
    """How strongly an identity is established. Voice alone never exceeds ``LOW``."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact an identity decision is based on, e.g. a speaker match or an HA user."""

    kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@payload_type("identity.principal/1", privileged_grant=IDENTITY_PROVIDER_GRANT, kind="value")
@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting in this turn and with which roles.

    Privileged: only nodes holding the ``identity.provider`` grant may emit it.
    """

    TYPE_ID: ClassVar[str]

    user_id: str | None
    roles: frozenset[str]
    assurance: Assurance
    evidence: tuple[Evidence, ...] = ()

    @classmethod
    def unknown(cls, *evidence: Evidence) -> Principal:
        return cls(user_id=None, roles=frozenset(), assurance=Assurance.NONE, evidence=evidence)

    @property
    def is_known(self) -> bool:
        return self.user_id is not None and self.assurance is not Assurance.NONE


# --- agent ---------------------------------------------------------------------------------------


@payload_type("agent.response/1", kind="value")
@dataclass(frozen=True, slots=True)
class Response:
    """Final agent response. ``speech_text`` overrides what is spoken; ``text`` is what is shown."""

    TYPE_ID: ClassVar[str]

    text: str
    speech_text: str | None = None
    language: str | None = None
    continue_conversation: bool = False
    data: Mapping[str, Any] | None = None

    @property
    def spoken(self) -> str:
        return self.text if self.speech_text is None else self.speech_text


# --- settings ------------------------------------------------------------------------------------


@payload_type("config.value/1", kind="value")
@dataclass(frozen=True, slots=True)
class SettingValue:
    """A value for one setting of a node, for one turn: what a setting made an input receives
    (``DYNAMIC_KEY``). The runtime turns it into the setting's type (``"0.8"`` for a number)."""

    TYPE_ID: ClassVar[str]

    value: str | int | float | bool
