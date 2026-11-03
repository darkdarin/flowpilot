"""Wyoming nodes against fake Wyoming services."""

from __future__ import annotations

from typing import Any

import pytest

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import AudioChunk, AudioFormat, Node, TextDelta
from flowpilot_wyoming import WyomingStt, WyomingTts, split_sentences

from .fake_servers import Record, asr, serve, tts

FMT = AudioFormat(sample_rate=16000)


def audio(n: int, *, end: str = "end") -> InputFeed:
    chunks = [AudioChunk(bytes(640), i * 320) for i in range(n)]
    return InputFeed(items=chunks, meta=FMT.as_meta(), end=end)  # type: ignore[arg-type]


async def started(cls: type[Node], config: dict[str, Any]) -> Node:
    node = cls()
    await node.start(RuntimeNodeContext(cls.spec.type, config, {}, frozenset()))
    return node


# --- sentence splitting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("buffer", "sentences", "rest"),
    [
        ("Hello there, my friend. How are", ["Hello there, my friend."], "How are"),
        ("It is 7 PM. ", [], "It is 7 PM. "),  # too short alone: waits for more text
        ("It is 7 PM. The lights are on. ", ["It is 7 PM. The lights are on."], ""),
        ("Done! «Quoted.» Next one here", ["Done! «Quoted.»"], "Next one here"),
        ("Line one is long enough\nline two", ["Line one is long enough"], "line two"),
        ("no end yet", [], "no end yet"),
    ],
)
def test_split_sentences(buffer: str, sentences: list[str], rest: str) -> None:
    assert split_sentences(buffer) == (sentences, rest)


# --- speech-to-text -------------------------------------------------------------------------------


async def test_stt_own_vad_streams_partials_and_stops_audio_early() -> None:
    rec = Record()
    async with serve(asr(rec, own_vad=True, streaming=True, end_after=15)) as uri:
        node = await started(WyomingStt, {"uri": uri, "language": "en"})
        assert node.streams_partials is True  # type: ignore[attr-defined]
        run = await run_node_once(node, {"audio": audio(200, end="hang")})
    assert run.error is None, run.error
    texts = [(t.text, t.final) for t in run.outputs["transcript"].items]
    assert texts == [
        ("turn ", False),
        ("turn on ", False),
        ("turn on the ", False),
        ("turn on the light", True),
    ]
    assert run.outputs["utterance"].items[0].text == "turn on the light"
    assert 15 <= rec.audio_chunks < 200  # stopped sending once the service finalized
    assert rec.transcribe[0].language == "en"


async def test_stt_external_vad_waits_for_audio_end() -> None:
    rec = Record()
    async with serve(asr(rec, own_vad=False, streaming=False, words=["hello"])) as uri:
        node = await started(WyomingStt, {"uri": uri})
        run = await run_node_once(node, {"audio": audio(20)})
    assert run.error is None, run.error
    assert [(t.text, t.final) for t in run.outputs["transcript"].items] == [("hello", True)]
    assert rec.audio_chunks == 20 and rec.audio_stopped


async def test_stt_error_event_is_a_node_error() -> None:
    rec = Record()
    async with serve(asr(rec, fail=True)) as uri:
        node = await started(WyomingStt, {"uri": uri})
        run = await run_node_once(node, {"audio": audio(5)})
    assert run.error is not None and "model exploded" in str(run.error)


async def test_stt_abrupt_hangup_is_a_clear_error() -> None:
    rec = Record()
    async with serve(asr(rec, fail="abrupt")) as uri:
        node = await started(WyomingStt, {"uri": uri})
        run = await run_node_once(node, {"audio": audio(50)})
    # Depending on TCP timing the error event survives or is discarded by the reset.
    assert run.error is not None
    assert "model exploded" in str(run.error) or "service" in str(run.error)
    assert "WyomingError" in type(run.error).__name__


async def test_stt_text_turn_does_not_connect() -> None:
    rec = Record()
    async with serve(asr(rec)) as uri:
        node = await started(WyomingStt, {"uri": uri})
        connections_after_describe = rec.connections
        run = await run_node_once(node, {})
    assert run.error is None
    assert rec.connections == connections_after_describe
    assert run.outputs["transcript"].items == []


async def test_stt_empty_final_has_no_utterance() -> None:
    rec = Record()
    async with serve(asr(rec, own_vad=False, streaming=False, words=[""])) as uri:
        node = await started(WyomingStt, {"uri": uri})
        run = await run_node_once(node, {"audio": audio(3)})
    assert run.error is None
    assert run.outputs["utterance"].items == []


# --- text-to-speech -------------------------------------------------------------------------------

DELTAS = ["Hello there, ", "my friend. How", " are you today? ", "Fine."]


def deltas(*, end: str = "end") -> InputFeed:
    return InputFeed(items=[TextDelta(d) for d in DELTAS], end=end)  # type: ignore[arg-type]


async def test_tts_sentence_mode_for_plain_services() -> None:
    rec = Record()
    async with serve(tts(rec, streaming=False)) as uri:
        node = await started(WyomingTts, {"uri": uri})
        assert node.streaming is False  # type: ignore[attr-defined]
        run = await run_node_once(node, {"text": deltas()})
    assert run.error is None, run.error
    assert rec.synthesize == ["Hello there, my friend.", "How are you today?", "Fine."]
    speech = run.outputs["speech"]
    assert speech.header is not None and speech.header.meta["sample_rate"] == 22050
    assert b"".join(c.pcm for c in speech.items) == b"".join(s.encode() for s in rec.synthesize)
    offsets = [c.sample_offset for c in speech.items]
    assert offsets == sorted(offsets) and offsets[0] == 0


async def test_tts_sentence_mode_speaks_before_text_ends() -> None:
    rec = Record()
    async with serve(tts(rec, streaming=False)) as uri:
        node = await started(WyomingTts, {"uri": uri})
        # The text stream never ends: complete sentences must still be spoken.
        run = await run_node_once(node, {"text": deltas(end="hang")}, cancel_after_s=0.5)
    assert rec.synthesize[:2] == ["Hello there, my friend.", "How are you today?"]
    assert run.outputs["speech"].items


async def test_tts_streaming_service() -> None:
    rec = Record()
    async with serve(tts(rec, streaming=True)) as uri:
        node = await started(WyomingTts, {"uri": uri})
        assert node.streaming is True  # type: ignore[attr-defined]
        run = await run_node_once(node, {"text": deltas()})
    assert run.error is None, run.error
    assert rec.chunks == DELTAS
    assert rec.synthesize == ["".join(DELTAS)]
    assert b"".join(c.pcm for c in run.outputs["speech"].items) == "".join(DELTAS).encode()


async def test_tts_error_event_is_a_node_error() -> None:
    rec = Record()
    async with serve(tts(rec, fail=True)) as uri:
        node = await started(WyomingTts, {"uri": uri})
        run = await run_node_once(node, {"text": deltas()})
    assert run.error is not None and "no voice" in str(run.error)


# --- conformance ----------------------------------------------------------------------------------


async def test_stt_conforms() -> None:
    async with serve(asr(Record())) as uri:
        report = await check_node(
            WyomingStt, config={"uri": uri, "language": "en"}, sample={"audio": audio(20)}
        )
    assert_conforms(report)


@pytest.mark.parametrize("streaming", [False, True])
async def test_tts_conforms(streaming: bool) -> None:
    async with serve(tts(Record(), streaming=streaming)) as uri:
        report = await check_node(WyomingTts, config={"uri": uri}, sample={"text": deltas()})
    assert_conforms(report)
