"""Payload type registry and built-in payloads."""

from dataclasses import FrozenInstanceError, dataclass
from typing import ClassVar

import pytest

from flowpilot_sdk import (
    IDENTITY_PROVIDER_GRANT,
    PRIVILEGED_TYPES,
    Assurance,
    AudioChunk,
    AudioFormat,
    Principal,
    Response,
    SpeakerMatch,
    SpeakerStatus,
    payload_class,
    payload_type,
    registered_types,
    type_id_of,
    validate_type_id,
)

BUILTIN_IDS = {
    "audio.pcm/1",
    "text.transcript/1",
    "text.utterance/1",
    "text.delta/1",
    "identity.speaker/1",
    "identity.principal/1",
    "agent.response/1",
}


def test_builtin_types_registered() -> None:
    assert BUILTIN_IDS <= set(registered_types())
    assert type_id_of(AudioChunk) == "audio.pcm/1"
    assert payload_class("identity.principal/1") is Principal


@pytest.mark.parametrize("bad", ["audio", "audio.pcm", "Audio.pcm/1", "audio.pcm/0", "audio/1"])
def test_invalid_type_ids_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_type_id(bad)


def test_duplicate_registration_rejected() -> None:
    with pytest.raises(ValueError, match="already registered"):

        @payload_type("audio.pcm/1")
        @dataclass(frozen=True, slots=True)
        class Other:
            TYPE_ID: ClassVar[str]


def test_unknown_lookups_raise() -> None:
    with pytest.raises(LookupError):
        payload_class("nope.nothing/1")
    with pytest.raises(LookupError):
        type_id_of(AudioFormat)


def test_principal_is_privileged() -> None:
    assert PRIVILEGED_TYPES["identity.principal/1"] == IDENTITY_PROVIDER_GRANT


def test_principal_unknown_has_no_rights() -> None:
    p = Principal.unknown()
    assert p.user_id is None
    assert p.roles == frozenset()
    assert p.assurance is Assurance.NONE
    assert not p.is_known


def test_payloads_are_immutable() -> None:
    chunk = AudioChunk(pcm=b"\x00\x00", sample_offset=0)
    with pytest.raises(FrozenInstanceError):
        chunk.sample_offset = 1  # type: ignore[misc]


def test_speaker_unknown_helper() -> None:
    m = SpeakerMatch.unknown("no_audio", model_id="m1")
    assert m.status is SpeakerStatus.UNKNOWN
    assert m.speaker_id is None
    assert m.reason == "no_audio"


def test_response_spoken_text() -> None:
    assert Response(text="see https://x").spoken == "see https://x"
    assert Response(text="see https://x", speech_text="see the link").spoken == "see the link"


def test_audio_format_meta_roundtrip() -> None:
    fmt = AudioFormat(sample_rate=16000)
    assert AudioFormat.from_meta(fmt.as_meta()) == fmt
    assert fmt.bytes_per_sample == 2
