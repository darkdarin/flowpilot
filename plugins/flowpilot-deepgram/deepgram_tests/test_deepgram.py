"""Deepgram nodes against a fake Deepgram (the Flux listen and speak WebSocket protocols)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_deepgram import DeepgramError, DeepgramStt, DeepgramTts, stt, tts
from flowpilot_sdk import AudioChunk, AudioFormat, TextDelta

KEY = "test-key"
CONFIG = {"api_key": {"secret": "deepgram"}}
FRAME = 2560  # 80 ms at 16 kHz


def word(text: str, start: float) -> dict[str, Any]:
    return {"word": text, "confidence": 0.9, "start": start, "end": start + 0.3}


def turn_info(event: str, words: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "type": "TurnInfo",
        "event": event,
        "turn_index": 0,
        "transcript": " ".join(w["word"] for w in words),
        "words": words,
        "end_of_turn_confidence": 0.8 if event == "EndOfTurn" else 0.1,
        "languages": ["ru"],
    } | extra


class FakeDeepgram:
    """Records what the nodes send; answers like Deepgram Flux."""

    def __init__(self) -> None:
        self.listen_query: list[tuple[str, str]] = []
        self.frames: list[int] = []
        self.script = "endpoint"  # endpoint | wake_word_pause | force | silence | error | 401
        self.control: list[str] = []
        self.speak_query: dict[str, str] = {}
        self.spoken: list[str] = []
        self.auth: list[str] = []

    async def listen(self, request: web.Request) -> web.StreamResponse:
        self.auth.append(request.headers.get("Authorization", ""))
        if self.script == "401":
            return web.Response(status=401)
        self.listen_query = list(request.query.items())
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "Connected", "request_id": "r", "sequence_id": 0})
        wake = [word("Алиса,", 0.1)]
        command = [word("Включи", 0.9), word("свет.", 1.3)]
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                self.frames.append(len(msg.data))
                n = len(self.frames)
                if self.script == "error" and n == 2:
                    await ws.send_json({"type": "Error", "code": "BAD", "description": "nope"})
                    break
                if self.script == "silence":
                    continue
                if n == 2:
                    await ws.send_json(turn_info("StartOfTurn", wake[:1]))
                if self.script == "wake_word_pause" and n == 4:
                    await ws.send_json(turn_info("EndOfTurn", wake, trigger="model"))
                if n == 6:
                    words = command if self.script == "wake_word_pause" else wake + command[:1]
                    await ws.send_json(turn_info("Update", words))
                if n == 8 and self.script in ("endpoint", "wake_word_pause"):
                    words = command if self.script == "wake_word_pause" else wake + command
                    await ws.send_json(turn_info("EndOfTurn", words, trigger="model"))
            elif msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                self.control.append(data["type"])
                if data["type"] == "ForceEndTurn":
                    if self.script == "silence":
                        await ws.send_json(
                            {"type": "Warning", "code": "FORCE_END_TURN_NO_ACTIVE_TURN"}
                        )
                    else:
                        await ws.send_json(turn_info("EndOfTurn", wake + command, trigger="manual"))
                if data["type"] == "CloseStream":
                    break
        return ws

    async def speak(self, request: web.Request) -> web.WebSocketResponse:
        self.speak_query = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "Connected", "model_name": request.query["model"]})
        text = ""
        async for msg in ws:
            data = json.loads(msg.data)
            self.control.append(data["type"])
            if data["type"] == "Speak":
                if not text:
                    await ws.send_json({"type": "SpeechStarted", "speech_id": "dg_sp_1"})
                text += data["text"]
                self.spoken.append(data["text"])
                await ws.send_bytes(bytes(2 * 100 * len(data["text"]) + 1))  # an odd byte
            elif data["type"] == "Flush":
                await ws.send_bytes(b"\x00")
                await ws.send_json({"type": "Flushed", "speech_id": "dg_sp_1"})
                await ws.send_json({"type": "SpeechMetadata", "speech_id": "dg_sp_1"})
            elif data["type"] == "Close":
                break
        return ws


@pytest.fixture
async def fake(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeDeepgram]:
    deepgram = FakeDeepgram()
    app = web.Application()
    app.router.add_get("/v2/listen", deepgram.listen)
    app.router.add_get("/v2/speak", deepgram.speak)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/").replace("http", "ws")
    monkeypatch.setattr(stt, "STT_URL", base + "/v2/listen")
    monkeypatch.setattr(tts, "TTS_URL", base + "/v2/speak")
    yield deepgram
    await server.close()


async def started(cls: type[Any], config: dict[str, Any]) -> Any:
    node = cls()
    ctx = RuntimeNodeContext("n", CONFIG | config, {"deepgram": KEY}, frozenset({"deepgram"}))
    await node.start(ctx)
    return node


def speech(ms: int, end: str = "hang") -> InputFeed:
    pcm = bytes(32 * ms)
    items = [AudioChunk(pcm[i : i + 640], i // 2) for i in range(0, len(pcm), 640)]
    meta = AudioFormat(sample_rate=16000).as_meta()
    return InputFeed(items=items, meta=meta, end=end)  # type: ignore[arg-type]


async def test_stt_ends_on_end_of_turn_and_cuts_the_wake_word(fake: FakeDeepgram) -> None:
    config = {"language": "ru", "wake_words": ["Алиса"], "keyterms": ["свет"]}
    node = await started(DeepgramStt, config)
    run = await run_node_once(node, {"audio": speech(2000)})  # the satellite would keep going
    assert run.error is None, run.error
    texts = [(t.text, t.final) for t in run.outputs["transcript"].items]
    assert texts[-1] == ("Включи свет.", True)
    assert ("Включи", False) in texts  # partials, the wake word already cut
    (utterance,) = run.outputs["utterance"].items
    assert (utterance.text, utterance.language) == ("Включи свет.", "ru")
    # the input never ends (end="hang"): the turn finished on Flux's EndOfTurn
    assert set(fake.frames) == {FRAME} and len(fake.frames) >= 8
    assert fake.auth == [f"Token {KEY}"]
    query = fake.listen_query
    assert ("model", "flux-general-multi") in query and ("language_hint", "ru") in query
    assert ("sample_rate", "16000") in query and ("encoding", "linear16") in query
    assert [v for k, v in query if k == "keyterm"] == ["свет", "Алиса"]
    assert fake.control == ["CloseStream"]
    (event,) = [e for e in run.trace.events if e.event == "deepgram.stt"]
    assert event.attrs["ended_by"] == "model" and event.attrs["wake_word_cut"] is True


async def test_stt_keeps_listening_after_the_wake_word_alone(fake: FakeDeepgram) -> None:
    fake.script = "wake_word_pause"
    node = await started(DeepgramStt, {"wake_words": ["Алиса"]})
    run = await run_node_once(node, {"audio": speech(2000)})
    assert run.error is None, run.error
    assert run.outputs["utterance"].items[0].text == "Включи свет."
    (event,) = [e for e in run.trace.events if e.event == "deepgram.stt"]
    assert event.attrs["turns"] == 2


async def test_stt_forces_the_end_of_turn_when_the_audio_ends(fake: FakeDeepgram) -> None:
    fake.script = "force"
    node = await started(DeepgramStt, {"model": "flux-general-en", "language": "ru"})
    run = await run_node_once(node, {"audio": speech(500, end="end")})
    assert run.error is None, run.error
    assert run.outputs["utterance"].items[0].text == "Алиса, Включи свет."
    assert fake.control == ["ForceEndTurn", "CloseStream"]
    assert not [k for k, _ in fake.listen_query if k == "language_hint"]  # English only model
    # 500 ms: six whole 80 ms frames, then the rest
    assert fake.frames == [FRAME] * 6 + [16000 - 6 * FRAME]


async def test_stt_without_speech_sends_no_utterance(fake: FakeDeepgram) -> None:
    fake.script = "silence"
    run = await run_node_once(await started(DeepgramStt, {}), {"audio": speech(300, end="end")})
    assert run.error is None, run.error
    assert [(t.text, t.final) for t in run.outputs["transcript"].items] == [("", True)]
    assert run.outputs["utterance"].items == []
    (event,) = [e for e in run.trace.events if e.event == "deepgram.stt"]
    assert event.attrs["ended_by"] == "no_speech"


async def test_stt_reports_deepgram_errors(fake: FakeDeepgram) -> None:
    fake.script = "error"
    run = await run_node_once(await started(DeepgramStt, {}), {"audio": speech(300)})
    assert isinstance(run.error, DeepgramError) and "BAD: nope" in str(run.error)
    fake.script = "401"
    run = await run_node_once(await started(DeepgramStt, {}), {"audio": speech(300)})
    assert isinstance(run.error, DeepgramError) and "invalid API key" in str(run.error)


async def test_tts_streams_speech(fake: FakeDeepgram) -> None:
    node = await started(DeepgramTts, {"voice": "flux-olga-ru", "sample_rate": 16000})
    deltas = [TextDelta("Привет"), TextDelta(""), TextDelta(", мир")]
    run = await run_node_once(node, {"text": InputFeed(items=deltas, meta={}, end="end")})
    assert run.error is None, run.error
    out = run.outputs["speech"]
    assert out.header is not None and out.header.meta["sample_rate"] == 16000
    # every odd byte joined the next frame: whole samples only
    assert all(len(c.pcm) % 2 == 0 for c in out.items)
    assert sum(len(c.pcm) for c in out.items) == 2 * 100 * len("Привет, мир") + 2
    assert fake.spoken == ["Привет", ", мир"]
    assert fake.control == ["Speak", "Speak", "Flush", "Close"]
    assert fake.speak_query == {
        "model": "flux-olga-ru",
        "encoding": "linear16",
        "sample_rate": "16000",
    }


async def test_tts_picks_a_voice_of_the_turn_language(fake: FakeDeepgram) -> None:
    node = await started(DeepgramTts, {"speed": 1.2})
    voice = node._voice("ru-RU")
    assert voice.endswith("-ru")
    assert node._voice(None) == "flux-hannah-en"
    run = await run_node_once(
        node, {"text": InputFeed(items=[TextDelta("Hi")], meta={}, end="end")}
    )
    assert run.error is None, run.error
    assert fake.speak_query["speed"] == "1.2"


async def test_tts_without_text_does_not_flush(fake: FakeDeepgram) -> None:
    node = await started(DeepgramTts, {})
    run = await run_node_once(node, {"text": InputFeed(items=[], meta={}, end="end")})
    assert run.error is None, run.error
    assert "Flush" not in fake.control


async def test_tts_rejects_bad_settings(fake: FakeDeepgram) -> None:
    with pytest.raises(DeepgramError, match="not a Flux voice"):
        await started(DeepgramTts, {"voice": "aura-2-thalia-en"})
    with pytest.raises(DeepgramError, match="no Flux voice for language"):
        await started(DeepgramTts, {"language": "xx"})


async def test_setting_choices() -> None:
    voices = await DeepgramTts.config_options("voice", {"language": "ru"})
    assert "flux-olga-ru" in [v.id for v in voices]
    assert all(v.id.endswith("-ru") for v in voices)
    olga = next(v for v in voices if v.id == "flux-olga-ru")
    assert (olga.name, olga.description) == ("Olga", "ru · female")
    assert len(await DeepgramTts.config_options("voice", {})) > 200
    assert "ru" in [o.id for o in await DeepgramStt.config_options("language", {})]


async def test_conforms(fake: FakeDeepgram) -> None:
    secrets = {"deepgram": KEY}
    fake.script = "force"
    stt_sample = {"audio": speech(400, end="end")}
    tts_sample = {"text": InputFeed(items=[TextDelta("Hi.")], meta={}, end="end")}
    assert_conforms(
        await check_node(DeepgramStt, config=CONFIG, secrets=secrets, sample=stt_sample)
    )
    assert_conforms(
        await check_node(DeepgramTts, config=CONFIG, secrets=secrets, sample=tts_sample)
    )
