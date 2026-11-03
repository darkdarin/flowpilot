"""Voice command segmentation."""

from __future__ import annotations

import math
from typing import Any

import pytest

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import AudioChunk, AudioFormat
from flowpilot_vad import (
    CommandSegmenter,
    EnergyDetector,
    FrameBuffer,
    MicroDetector,
    Outcome,
    VadSegmenter,
)

FMT = AudioFormat(sample_rate=16000)
CHUNK_SAMPLES = 320  # 20 ms chunks, like satellites send


def tone(ms: int, amplitude: int = 8000) -> bytes:
    n = 16 * ms
    samples = (int(amplitude * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(n))
    return b"".join(s.to_bytes(2, "little", signed=True) for s in samples)


def silence(ms: int) -> bytes:
    return bytes(32 * ms)


def chunks(pcm: bytes) -> list[AudioChunk]:
    size = CHUNK_SAMPLES * 2
    return [AudioChunk(pcm[i : i + size], i // 2) for i in range(0, len(pcm), size)]


def feed(pcm: bytes, *, end: str = "hang") -> InputFeed:
    return InputFeed(items=chunks(pcm), meta=FMT.as_meta(), end=end)  # type: ignore[arg-type]


async def node(config: dict[str, Any]) -> VadSegmenter:
    n = VadSegmenter()
    await n.start(RuntimeNodeContext("vad", config, {}, frozenset()))
    return n


ENERGY = {"engine": "energy"}


def test_segmenter_ends_after_speech_and_silence() -> None:
    seg = CommandSegmenter()
    outcomes = [seg.process(0.01, 1.0) for _ in range(120)]  # 1.2 s speech
    outcomes += [seg.process(0.01, 0.0) for _ in range(69)]
    assert set(outcomes) == {Outcome.CONTINUE}
    assert seg.process(0.01, 0.0) is Outcome.END  # 0.7 s of silence


def test_segmenter_short_noise_does_not_start_command() -> None:
    seg = CommandSegmenter(timeout_s=2.0)
    for _ in range(10):  # 0.1 s blips separated by long silence
        for _ in range(10):
            seg.process(0.01, 1.0)
        for _ in range(110):
            if seg.process(0.01, 0.0) is not Outcome.CONTINUE:
                break
    assert not seg.in_command


def test_energy_detector() -> None:
    det = EnergyDetector()
    assert det.probability(silence(10)) == 0.0
    assert det.probability(tone(10)) == 1.0


def test_frame_buffer_recuts_chunks() -> None:
    buf = FrameBuffer()
    assert buf.frames(bytes(500)) == [bytes(320)]
    assert buf.frames(bytes(500)) == [bytes(320), bytes(320)]
    assert buf.frames(bytes(280)) == [bytes(320)]  # 40 left over + 280


def test_micro_detector_on_silence() -> None:
    det = MicroDetector()
    probabilities = [det.probability(silence(10)) for _ in range(50)]
    assert max(probabilities) < 0.2


async def test_node_ends_stream_after_command() -> None:
    pcm = silence(300) + tone(1200) + silence(2000)
    run = await run_node_once(await node(ENERGY), {"audio": feed(pcm)})
    assert run.error is None, run.error
    out = run.outputs["audio"]
    assert out.terminal == "end" and out.header is not None
    forwarded_ms = sum(len(c.pcm) for c in out.items) // 32
    # Speech ends at 1500 ms; the command ends after 0.7 s of silence (+ one 20 ms chunk).
    assert 2180 <= forwarded_ms <= 2240
    assert [c.sample_offset for c in out.items] == [c.sample_offset for c in chunks(pcm)][
        : len(out.items)
    ]
    events = [e.event for e in run.trace.events]
    assert events == ["vad.speech_start", "vad.end"]


async def test_node_times_out_without_speech() -> None:
    run = await run_node_once(
        await node(ENERGY | {"timeout_s": 0.5}), {"audio": feed(silence(2000))}
    )
    assert run.error is None
    assert sum(len(c.pcm) for c in run.outputs["audio"].items) // 32 == 500
    assert [e.event for e in run.trace.events] == ["vad.timeout"]


async def test_node_passes_through_until_input_ends() -> None:
    run = await run_node_once(await node(ENERGY), {"audio": feed(tone(400), end="end")})
    assert run.error is None
    assert b"".join(c.pcm for c in run.outputs["audio"].items) == tone(400)


async def test_node_rejects_other_formats() -> None:
    meta = AudioFormat(sample_rate=22050).as_meta()
    run = await run_node_once(
        await node(ENERGY), {"audio": InputFeed(items=chunks(tone(100)), meta=meta)}
    )
    assert isinstance(run.error, ValueError)


@pytest.mark.parametrize("config", [ENERGY, {}])
async def test_conforms(config: dict[str, Any]) -> None:
    sample = {"audio": feed(silence(200) + tone(600) + silence(1000), end="end")}
    assert_conforms(await check_node(VadSegmenter, config=config, sample=sample))
