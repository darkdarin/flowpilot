"""Gain and automatic gain control on synthetic speech-like signals."""

from __future__ import annotations

import math
from typing import Any

import pytest

from flowpilot_audio import AutoGain, Gain, GainControl
from flowpilot_audio.pcm import dbfs, peak, rms, samples
from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import AudioChunk, AudioFormat

FMT = AudioFormat(sample_rate=16000)
CHUNK = 320  # 20 ms


def tone(ms: int, level_dbfs: float) -> bytes:
    """A 220 Hz sine whose RMS is ``level_dbfs``."""
    amplitude = 32768 * 10 ** (level_dbfs / 20) * math.sqrt(2)
    n = 16 * ms
    raw = (round(amplitude * math.sin(2 * math.pi * 220 * i / 16000)) for i in range(n))
    values = (max(-32768, min(32767, v)) for v in raw)
    return b"".join(v.to_bytes(2, "little", signed=True) for v in values)


def silence(ms: int) -> bytes:
    return bytes(32 * ms)


def feed(pcm: bytes) -> InputFeed:
    size = CHUNK * 2
    items = [AudioChunk(pcm[i : i + size], i // 2) for i in range(0, len(pcm), size)]
    return InputFeed(items=items, meta=FMT.as_meta(), end="end")


async def start(cls: type[Any], config: dict[str, Any]) -> Any:
    node = cls()
    await node.start(RuntimeNodeContext("n", config, {}, frozenset()))
    return node


def level(pcm: bytes) -> float:
    return dbfs(rms(samples(pcm)))


def out_pcm(run: Any) -> bytes:
    return b"".join(c.pcm for c in run.outputs["audio"].items)


async def test_gain_raises_the_level_and_keeps_timing() -> None:
    pcm = tone(400, -40)
    run = await run_node_once(await start(Gain, {"gain_db": 12}), {"audio": feed(pcm)})
    assert run.error is None and run.outputs["audio"].terminal == "end"
    assert level(out_pcm(run)) == pytest.approx(-28, abs=0.1)
    assert [c.sample_offset for c in run.outputs["audio"].items] == list(range(0, 6400, CHUNK))
    (event,) = run.trace.events
    assert event.event == "audio.gain" and event.attrs["clipped"] == 0
    assert event.attrs["in_rms_dbfs"] == pytest.approx(-40, abs=0.1)


async def test_gain_saturates_instead_of_wrapping() -> None:
    pcm = tone(200, -6)
    run = await run_node_once(await start(Gain, {"gain_db": 20}), {"audio": feed(pcm)})
    out = samples(out_pcm(run))
    assert peak(out) == 32768 or max(out) == 32767  # clipped at full scale, never wrapped
    assert run.trace.events[0].attrs["clipped"] > 0


async def test_agc_raises_quiet_speech_to_the_target() -> None:
    pcm = silence(200) + tone(2000, -42)
    run = await run_node_once(await start(AutoGain, {}), {"audio": feed(pcm)})
    assert run.error is None
    out = out_pcm(run)
    assert len(out) == len(pcm)
    assert level(out[-32 * 1000 :]) == pytest.approx(-20, abs=1)  # the last second: on target
    assert level(out[: 32 * 200]) == dbfs(0)  # silence stays silence
    attrs = run.trace.events[0].attrs
    assert attrs["in_rms_dbfs"] == pytest.approx(-42, abs=0.5) and attrs["speech"] is True


async def test_agc_lowers_loud_speech_without_clipping() -> None:
    pcm = tone(1500, -4)
    run = await run_node_once(await start(AutoGain, {"max_cut_db": 20}), {"audio": feed(pcm)})
    out = out_pcm(run)
    assert level(out[-32 * 500 :]) == pytest.approx(-20, abs=1)
    assert run.trace.events[0].attrs["clipped"] == 0


def test_agc_leaves_background_alone_and_limits_peaks() -> None:
    control = GainControl()
    speech, hiss = samples(tone(100, -40)), samples(tone(100, -65))
    for i in range(0, len(speech), 160):
        control.process(speech[i : i + 160], 10)
    gain = control.gain_db
    assert gain == pytest.approx(20, abs=0.5)
    for i in range(0, len(hiss), 160):  # a pause with background noise: the gain holds
        control.process(hiss[i : i + 160], 10)
    assert control.gain_db == gain
    shout = samples(tone(20, -3))
    out = control.process(shout[:160], 10)  # a sudden shout at +20 dB: the limiter catches it
    assert peak(out) <= 32767 * 10 ** (-1 / 20) + 1
    assert control.limited >= 1 and control.levels.clipped == 0


def test_agc_first_speech_sets_the_gain_at_once() -> None:
    control = GainControl()
    first = samples(tone(10, -36))
    control.process(first, 10)
    assert control.gain_db == pytest.approx(16, abs=0.5)  # no slow ramp at the start of a command


async def test_rejects_other_encodings() -> None:
    meta = {"encoding": "f32le", "sample_rate": 16000, "channels": 1}
    run = await run_node_once(
        await start(AutoGain, {}), {"audio": InputFeed(items=[], meta=meta, end="end")}
    )
    assert isinstance(run.error, ValueError)


@pytest.mark.parametrize("cls", [Gain, AutoGain])
async def test_conforms(cls: type[Any]) -> None:
    sample = {"audio": feed(silence(100) + tone(400, -30))}
    assert_conforms(await check_node(cls, config={}, sample=sample))
