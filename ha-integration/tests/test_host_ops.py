"""Host operations for speech: STT entities with streamed audio in, TTS with streamed audio out."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from custom_components.flowpilot.client import HostRequest
from custom_components.flowpilot.host import build_host_ops
from custom_components.flowpilot.wav import WavError, open_wav
from homeassistant.components import stt, tts
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


def make_wav(pcm: bytes, *, rate: int = 22050, bits: int = 16, streaming: bool = False,
             extra_chunk: bool = False) -> bytes:  # fmt: skip
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * bits // 8, bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra_chunk:
        info = b"INFOISFT\x05\x00\x00\x00ffmp\x00\x00"  # odd size: padded to a word boundary
        chunks += b"LIST" + struct.pack("<I", len(info) - 1) + info
    size = 0xFFFFFFFF if streaming else len(pcm)
    chunks += b"data" + struct.pack("<I", size)
    return b"RIFF" + struct.pack("<I", 4 + len(chunks) + len(pcm)) + b"WAVE" + chunks + pcm


async def pieces(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.mark.parametrize(("streaming", "extra"), [(False, False), (True, False), (True, True)])
async def test_open_wav(streaming: bool, extra: bool) -> None:
    pcm = bytes(range(256)) * 4
    fmt, stream = await open_wav(pieces(make_wav(pcm, streaming=streaming, extra_chunk=extra)))
    assert (fmt.sample_rate, fmt.channels, fmt.bits) == (22050, 1, 16)
    assert b"".join([c async for c in stream]) == pcm


async def test_open_wav_rejects_non_pcm16() -> None:
    with pytest.raises(WavError, match="16-bit"):
        await open_wav(pieces(make_wav(bytes(10), bits=8)))
    with pytest.raises(WavError, match="RIFF"):
        await open_wav(pieces(b"ID3" + bytes(40)))


def ops(hass: HomeAssistant) -> dict[str, Any]:
    return build_host_ops(hass, MockConfigEntry(domain="flowpilot"))


async def audio_in(chunks: int) -> AsyncIterator[bytes]:
    for _ in range(chunks):
        yield bytes(640)


class FakeSttEntity:
    def __init__(self) -> None:
        self.metadata: stt.SpeechMetadata | None = None
        self.received = 0

    def check_metadata(self, metadata: stt.SpeechMetadata) -> bool:
        return True

    async def internal_async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        self.metadata = metadata
        self.received = sum([len(c) async for c in stream])
        return stt.SpeechResult("turn on the light", stt.SpeechResultState.SUCCESS)


async def test_stt_process(hass: HomeAssistant) -> None:
    engine = FakeSttEntity()
    request = HostRequest(
        None,
        {"entity_id": "stt.cloud", "language": "en",
         "audio": {"stream": 1, "format": {"sample_rate": 16000, "channels": 1}}},
        audio=audio_in(10),
    )  # fmt: skip
    with patch.object(stt, "async_get_speech_to_text_entity", return_value=engine) as get:
        result = await ops(hass)["ha.stt.process"](request)
    get.assert_called_once_with(hass, "stt.cloud")
    assert result == {"text": "turn on the light", "state": "success"}
    assert engine.received == 6400
    assert engine.metadata is not None and engine.metadata.sample_rate == 16000
    assert engine.metadata.language == "en"


async def test_stt_process_refuses_flowpilot_stt(hass: HomeAssistant) -> None:
    entity = er.async_get(hass).async_get_or_create("stt", "flowpilot", "entry-stt")
    request = HostRequest(None, {"entity_id": entity.entity_id, "audio": {"stream": 1}})
    with pytest.raises(Exception, match="FlowPilot entity"):
        await ops(hass)["ha.stt.process"](request)


async def test_tts_synthesize_streams_pcm_back(hass: HomeAssistant) -> None:
    pcm = bytes(range(200)) * 10
    stream = MagicMock()

    async def result() -> AsyncIterator[bytes]:
        async for piece in pieces(make_wav(pcm, rate=16000, streaming=True), size=333):
            yield piece

    stream.async_stream_result = result
    sent: dict[str, Any] = {}

    async def send_audio(fmt: Mapping[str, Any], chunks: AsyncIterable[bytes]) -> None:
        sent["format"] = dict(fmt)
        sent["pcm"] = b"".join([c async for c in chunks])

    request = HostRequest(
        None,
        {"engine": "tts.cloud", "text": "Hello there.", "language": "en", "voice": "anna"},
        send_audio=send_audio,
    )
    with patch.object(tts, "async_create_stream", return_value=stream) as create:
        result_data = await ops(hass)["ha.tts.synthesize"](request)
    _, engine, language, options = create.call_args.args
    assert (engine, language) == ("tts.cloud", "en")
    assert options[tts.ATTR_PREFERRED_FORMAT] == "wav"
    assert options[tts.ATTR_PREFERRED_SAMPLE_RATE] == 16000
    assert options[tts.ATTR_VOICE] == "anna"
    stream.async_set_message.assert_called_once_with("Hello there.")
    assert sent["format"] == {"encoding": "s16le", "sample_rate": 16000, "channels": 1}
    assert sent["pcm"] == pcm
    assert result_data == {"sample_rate": 16000}
