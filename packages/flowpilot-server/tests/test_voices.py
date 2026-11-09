"""Voice profiles and the Who speaks node, with a stand-in for the speaker model.

The real models are downloaded on demand; here a "voice" is the loudness of a tone and its
embedding a vector made from it, so matching, margins and the node's flow are checked exactly.
"""

from __future__ import annotations

import io
import math
import stat
import wave
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_core.conformance import InputFeed, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import (
    Assurance,
    AudioChunk,
    AudioFormat,
    Principal,
    SpeakerStatus,
)
from flowpilot_server.speaker import SpeakerId
from flowpilot_server.voices import (
    TARGET_DB,
    VoiceError,
    VoiceStore,
    leveled,
    pcm_floats,
    speech_level_db,
)

RATE = 16000
PAVEL, MASHA = 120.0, 400.0  # the "voices": the pitch each one speaks at, Hz


def speech(pitch: float, seconds: float = 2.0, volume: float = 0.3, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return b"".join(
        int(volume * 32767 * math.sin(2 * math.pi * pitch * i / rate)).to_bytes(
            2, "little", signed=True
        )
        for i in range(n)
    )


async def fake_embed(self: VoiceStore, samples: list[float], sample_rate: int) -> list[float]:
    """A point on a circle: the higher the voice, the further round (loudness does not count)."""
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    pitch = crossings / 2 / (len(samples) / sample_rate)
    return [math.cos(pitch / 100), math.sin(pitch / 100)]


@pytest.fixture(autouse=True)
def model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(VoiceStore, "_embed", fake_embed)


async def enrolled(path: Path | None = None) -> VoiceStore:
    store = VoiceStore(path, None)
    for _ in range(2):
        await store.add_sample("u-pavel", speech(PAVEL), RATE, source="microphone")
        await store.add_sample("u-masha", speech(MASHA), RATE, source="recording")
    return store


async def test_a_voice_matches_its_person_and_a_stranger_does_not(tmp_path: Path) -> None:
    store = await enrolled(tmp_path / "voices.json")
    match = await store.identify(speech(PAVEL), RATE)
    assert (match.status, match.speaker_id) == (SpeakerStatus.MATCHED, "u-pavel")
    assert (await store.identify(speech(MASHA), RATE)).speaker_id == "u-masha"
    stranger = await store.identify(speech(260), RATE)  # far from both
    assert stranger.status is SpeakerStatus.UNKNOWN and stranger.speaker_id is None
    # kept with mode 600, embeddings and all, but no audio
    path = tmp_path / "voices.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = VoiceStore(path, None)
    assert (await again.identify(speech(PAVEL), RATE)).speaker_id == "u-pavel"
    assert [s["source"] for s in again.people()["u-masha"]] == ["recording", "recording"]
    assert "embedding" not in again.people()["u-masha"][0]


async def test_a_quiet_satellite_and_a_loud_microphone_sound_the_same() -> None:
    """A Yandex Station sends very quiet audio: speech is levelled before the model hears it."""
    store = VoiceStore(None, None)
    sample = await store.add_sample(
        "u-pavel", speech(PAVEL, volume=0.003), RATE, source="recording"
    )  # about -53 dB
    assert sample["level_db"] < -50
    match = await store.identify(speech(PAVEL, volume=0.5), RATE)
    assert (match.status, match.speaker_id) == (SpeakerStatus.MATCHED, "u-pavel")


def test_speech_is_brought_to_one_level_without_clipping() -> None:
    quiet = [0.01 * math.sin(i / 3) for i in range(16000)]
    louder, level = leveled(quiet, RATE)
    assert -45 < level < -40 and abs(speech_level_db(louder, RATE) - TARGET_DB) < 1
    loud = [0.9 * math.sin(i / 3) for i in range(16000)]
    assert max(abs(s) for s in leveled(loud, RATE)[0]) <= 0.99


async def test_too_close_to_two_people_is_unknown() -> None:
    store = VoiceStore(None, None)
    await store.add_sample("u-ann", speech(200), RATE, source="microphone")
    await store.add_sample("u-kate", speech(215), RATE, source="microphone")  # sounds alike
    match = await store.identify(speech(207), RATE)  # close enough to both
    assert match.status is SpeakerStatus.UNKNOWN and match.reason == "too close to two people"
    store.set_settings({"margin": 0})
    assert (await store.identify(speech(207), RATE)).status is SpeakerStatus.MATCHED


async def test_samples_are_checked() -> None:
    store = VoiceStore(None, None)
    with pytest.raises(VoiceError, match="too short"):
        await store.add_sample("u", speech(PAVEL, 0.5), RATE, source="microphone")
    with pytest.raises(VoiceError, match="too quiet"):
        await store.add_sample("u", speech(PAVEL, volume=0.0002), RATE, source="microphone")
    with pytest.raises(VoiceError, match="nobody has a voice profile"):
        await store.scores(speech(PAVEL), RATE)


async def test_samples_and_people_are_removed() -> None:
    store = await enrolled()
    first = store.people()["u-pavel"][0]["id"]
    assert store.remove_sample("u-pavel", first) and len(store.people()["u-pavel"]) == 1
    assert store.remove_person("u-masha") and "u-masha" not in store.people()
    assert not store.remove_person("u-masha")


async def test_another_model_has_its_own_threshold_and_profiles() -> None:
    store = await enrolled()
    store.set_settings({"threshold": 0.95})
    store.set_settings({"model": "3dspeaker-campplus"})
    assert store.threshold == 0.65  # back to the new model's own
    assert all(not s["current"] for s in store.people()["u-pavel"])  # samples of the other model
    match = await store.identify(speech(PAVEL), RATE)
    assert match.status is SpeakerStatus.ERROR  # nobody has samples of this model yet
    with pytest.raises(VoiceError):
        store.set_settings({"model": "nope"})


# ---- the node --------------------------------------------------------------------------------


async def node_with(voices: VoiceStore | None, **config: Any) -> SpeakerId:
    node = SpeakerId()
    ctx = RuntimeNodeContext("who", config, {}, frozenset(), voices=voices)
    await node.start(ctx)
    return node


def audio(pcm: bytes, end: str = "end") -> InputFeed:
    chunks = [AudioChunk(pcm[i : i + 640], i // 2) for i in range(0, len(pcm), 640)]
    return InputFeed(items=chunks, meta=AudioFormat(sample_rate=RATE).as_meta(), end=end)  # type: ignore[arg-type]


async def test_the_node_tells_who_speaks_and_reads_all_the_audio() -> None:
    node = await node_with(await enrolled(), seconds=1)
    long_speech = speech(PAVEL, 4.0)  # more than the node listens to: the rest is read too
    run = await run_node_once(
        node, {"audio": audio(long_speech), "principal": InputFeed(items=[Principal.unknown()])}
    )
    assert run.error is None, run.error
    (who,) = run.outputs["principal"].items
    assert who.user_id == "u-pavel" and who.assurance is Assurance.LOW and who.is_known
    (match,) = run.outputs["match"].items
    assert match.status is SpeakerStatus.MATCHED and match.score is not None


async def test_a_person_the_client_names_is_passed_on() -> None:
    node = await node_with(await enrolled())
    client = Principal("u-masha", frozenset(), Assurance.MEDIUM)
    run = await run_node_once(
        node, {"audio": audio(speech(PAVEL)), "principal": InputFeed(items=[client])}
    )
    assert run.error is None, run.error
    assert run.outputs["principal"].items == [client]  # the voice is not checked


async def test_without_voices_or_speech_the_speaker_is_unknown() -> None:
    run = await run_node_once(await node_with(None), {"audio": audio(speech(PAVEL))})
    assert run.error is None and not run.outputs["principal"].items[0].is_known
    run = await run_node_once(await node_with(await enrolled()), {})  # a typed turn
    assert not run.outputs["principal"].items[0].is_known
    assert run.outputs["match"].items[0].status is SpeakerStatus.UNKNOWN


async def test_no_time_left_is_unknown() -> None:
    node = await node_with(await enrolled(), seconds=3, timeout_s=1)
    run = await run_node_once(node, {"audio": audio(speech(PAVEL, 0.5), end="hang")}, budget_s=3)
    assert run.outputs["match"].items[0].status is SpeakerStatus.TIMEOUT
    assert not run.outputs["principal"].items[0].is_known


def test_pcm_reads_as_floats() -> None:
    assert pcm_floats((16384).to_bytes(2, "little", signed=True)) == [0.5]


# ---- the editor's API --------------------------------------------------------------------------


def wav(pcm: bytes, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


async def test_editor_api_enrolls_and_tests_voices(tmp_path: Path) -> None:
    from flowpilot_core import NodeRegistry  # noqa: PLC0415
    from flowpilot_core.builtin import register_builtin  # noqa: PLC0415
    from flowpilot_server.manager import GraphManager, GraphStore  # noqa: PLC0415
    from flowpilot_server.nodes import ClientSink, ClientSource  # noqa: PLC0415
    from flowpilot_server.probe import recordings_dir  # noqa: PLC0415
    from flowpilot_server.server import FlowPilotServer  # noqa: PLC0415

    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink):
        registry.register(cls)
    manager = GraphManager(
        registry, store=GraphStore(tmp_path / "graphs"), data_dir=tmp_path,
        voices=VoiceStore(tmp_path / "voices.json", None),
    )  # fmt: skip
    rec = recordings_dir(tmp_path)
    rec.mkdir(parents=True)
    (rec / "20261109T120000Z.main.probe.T1.wav").write_bytes(wav(speech(MASHA)))
    server = FlowPilotServer(token="t", default_graph="main", manager=manager)
    ts = TestServer(server.app())
    await ts.start_server()
    auth = {"Authorization": "Bearer t"}
    try:
        async with aiohttp.ClientSession(
            base_url=str(ts.make_url("/api/editor/")), headers=auth
        ) as http:
            as_wav = {"Content-Type": "audio/wav"}
            recording = {"recording": "20261109T120000Z.main.probe.T1.wav"}
            for _ in range(2):
                resp = await http.post(
                    "voices/u-pavel/samples", data=wav(speech(PAVEL)), headers=as_wav
                )
                assert resp.status == 200, await resp.text()
                resp = await http.post("voices/u-masha/samples", json=recording)
                assert resp.status == 200, await resp.text()
            short = wav(speech(PAVEL, 0.3))
            bad = await http.post("voices/u-pavel/samples", data=short, headers=as_wav)
            assert bad.status == 400 and "too short" in (await bad.json())["error"]
            listed = await (await http.get("voices")).json()
            counts = {k: len(v) for k, v in listed["profiles"].items()}
            assert counts == {"u-pavel": 2, "u-masha": 2}
            assert listed["model"] == "wespeaker-resnet34" and listed["threshold"] == 0.8
            resp = await http.post("voices/test", data=wav(speech(MASHA)), headers=as_wav)
            test = await resp.json()
            assert test["match"] == "u-masha" and set(test["scores"]) == {"u-pavel", "u-masha"}
            put = await http.put("voices", json={"threshold": 0.9, "margin": 0.1})
            assert (await put.json())["threshold"] == 0.9
            sample = listed["profiles"]["u-pavel"][0]["id"]
            gone = await http.delete(f"voices/u-pavel/samples/{sample}")
            assert gone.status == 200
        async with aiohttp.ClientSession() as anonymous:
            assert (await anonymous.get(ts.make_url("/api/editor/voices"))).status == 401
    finally:
        await ts.close()
        await manager.stop()
