"""Sound signal: a short sound on speech when anything comes to it."""

from __future__ import annotations

import base64
import io
import wave
from typing import Any

from flowpilot_core.builtin import TextFirst
from flowpilot_core.conformance import InputFeed, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import AudioChunk, AudioFormat, Response
from flowpilot_server.signal import SoundSignal, default_sound


def wav(pcm: bytes, rate: int = 8000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


async def play(config: dict[str, Any]) -> Any:
    node = SoundSignal()
    await node.start(RuntimeNodeContext("ding", config, {}, frozenset()))
    done = InputFeed(items=[Response(text="Включила свет")], meta={}, end="end")
    run = await run_node_once(node, {"trigger": done})
    assert run.error is None, run.error
    return run.outputs["speech"]


async def test_home_assistants_sound_by_default() -> None:
    fmt, pcm = default_sound()
    assert fmt.sample_rate == 22050 and 1.0 < len(pcm) / (2 * 22050) < 2.0
    speech = await play({"voice_only": False})
    assert AudioFormat.from_meta(speech.header.meta).sample_rate == 22050
    assert b"".join(c.pcm for c in speech.items) == pcm


async def test_a_sound_of_ones_own() -> None:
    speech = await play({"voice_only": False, "sound": wav(b"\x01\x00" * 800)})
    assert AudioFormat.from_meta(speech.header.meta).sample_rate == 8000
    assert b"".join(c.pcm for c in speech.items) == b"\x01\x00" * 800
    assert [c.sample_offset for c in speech.items] == [0]


async def test_silent_in_typed_turns() -> None:
    speech = await play({})  # the test turn is a typed one
    assert speech.items == []


async def test_first_present_passes_audio_on_with_its_format() -> None:
    node = TextFirst()
    await node.start(RuntimeNodeContext("join", {}, {}, frozenset()))
    sound = InputFeed(
        items=[AudioChunk(b"\x00\x00" * 4, 0)], meta={"sample_rate": 22050}, end="end"
    )
    run = await run_node_once(node, {"primary": sound, "fallback": InputFeed(end="absent")})
    assert run.error is None, run.error
    header = run.outputs["value"].header
    assert header is not None and header.meta == {"sample_rate": 22050}
    assert header.type == "audio.pcm/1"
    assert [c.pcm for c in run.outputs["value"].items] == [b"\x00\x00" * 4]
