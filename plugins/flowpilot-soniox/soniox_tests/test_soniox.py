"""Soniox nodes against a fake Soniox (the real-time WebSocket protocols and the model lists)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import AudioChunk, AudioFormat, TextDelta
from flowpilot_soniox import SonioxError, SonioxStt, SonioxTts, common, stt, tts

KEY = "test-key"
MINE = "497f6eca-6276-4993-bfeb-53cbbbba6f08"  # a cloned voice, ready for tts-rt-v1
OLD = "0a6c7d1e-0000-4000-8000-000000000001"  # cloned before the model existed
CONFIG = {"api_key": {"secret": "soniox"}}


class FakeSoniox:
    """Records what the nodes send; answers like Soniox."""

    def __init__(self) -> None:
        self.stt_config: dict[str, Any] = {}
        self.stt_audio = 0
        self.stt_script = "endpoint"  # endpoint | finalize | error
        self.languages: dict[str, str] = {}  # the language of a final token, by its text
        self.tts_configs: list[dict[str, Any]] = []
        self.tts_text: dict[str, str] = {}
        self.tts_timeout_first = False
        self.auth: list[str] = []

    async def stt(self, request: web.Request) -> web.WebSocketResponse:
        self.auth.append(request.headers.get("Authorization", ""))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if "model" in data:
                    self.stt_config = data
                elif data == {"type": "finalize"}:
                    await ws.send_json({"tokens": [self.final(" свет"), self.final("<fin>")]})
            elif msg.type == WSMsgType.BINARY:
                self.stt_audio += len(msg.data)
                if self.stt_script == "error":
                    await ws.send_json({"error_code": 401, "error_message": "bad key"})
                    break
                if self.stt_audio == 3200:  # 100 ms in: a partial result
                    await ws.send_json({"tokens": [{"text": " Алиса", "is_final": False}]})
                if self.stt_audio == 6400:
                    await ws.send_json(
                        {"tokens": [self.final(" Алиса", 0), self.final(" включи", 900)]}
                    )
                if self.stt_audio == 9600 and self.stt_script == "endpoint":
                    await ws.send_json({"tokens": [self.final(" свет"), self.final("<end>")]})
        return ws

    def final(self, text: str, start_ms: int = 1200) -> dict[str, Any]:
        token = {"text": text, "is_final": True, "start_ms": start_ms, "end_ms": start_ms + 300}
        if language := self.languages.get(text.strip()):
            token["language"] = language
        return token

    async def tts(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            data = json.loads(msg.data)
            sid = data["stream_id"]
            if "voice" in data:
                self.tts_configs.append(data)
                self.tts_text[sid] = ""
                continue
            self.tts_text[sid] += data["text"]
            if self.tts_timeout_first and len(self.tts_configs) == 1:
                await ws.send_json({"stream_id": sid, "audio": self.audio(data["text"])})
                await ws.send_json(
                    {"stream_id": sid, "error_type": "request_timeout", "terminated": True}
                )
                continue
            if data["text_end"]:
                audio = self.audio(self.tts_text[sid])
                await ws.send_json({"stream_id": sid, "audio": audio, "terminated": True})
        return ws

    @staticmethod
    def audio(text: str) -> str:
        return base64.b64encode(bytes(2 * 100 * len(text))).decode()  # 100 samples a character

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response({"models": [{"id": "stt-rt-v5", "name": "Real-time v5"}]})

    async def voices(self, request: web.Request) -> web.Response:
        def voice(uuid: str, name: str, status: str) -> dict[str, Any]:
            return {"id": uuid, "name": name, "models": [{"model": "tts-rt-v1", "status": status}]}

        if request.query.get("cursor") != "page2":
            page = [voice(MINE, "Моя бабушка", "ready")]
            return web.json_response({"voices": page, "next_page_cursor": "page2"})
        return web.json_response({"voices": [voice(OLD, "Old", "not_computed")]})

    async def tts_models(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {KEY}":
            return web.json_response({"error": "unauthorized"}, status=401)
        voices = [{"id": "Maya", "gender": "female"}, {"id": "Adrian", "gender": "male"}]
        model = {
            "id": "tts-rt-v1",
            "voices": voices,
            "languages": [{"code": "en"}, {"code": "ru"}],
        }
        return web.json_response({"models": [model]})


@pytest.fixture
async def fake(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeSoniox]:
    soniox = FakeSoniox()
    app = web.Application()
    app.router.add_get("/stt", soniox.stt)
    app.router.add_get("/tts", soniox.tts)
    app.router.add_get("/v1/models", soniox.models)
    app.router.add_get("/v1/tts-models", soniox.tts_models)
    app.router.add_get("/v1/voices", soniox.voices)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(stt, "STT_URL", base.replace("http", "ws") + "/stt")
    monkeypatch.setattr(tts, "TTS_URL", base.replace("http", "ws") + "/tts")
    monkeypatch.setattr(common, "API_URL", base + "/v1")
    yield soniox
    await server.close()


async def started(cls: type[Any], config: dict[str, Any]) -> Any:
    node = cls()
    ctx = RuntimeNodeContext("n", CONFIG | config, {"soniox": KEY}, frozenset({"soniox"}))
    await node.start(ctx)
    return node


def speech(ms: int, end: str = "hang") -> InputFeed:
    pcm = bytes(32 * ms)
    items = [AudioChunk(pcm[i : i + 640], i // 2) for i in range(0, len(pcm), 640)]
    meta = AudioFormat(sample_rate=16000).as_meta()
    return InputFeed(items=items, meta=meta, end=end)  # type: ignore[arg-type]


async def test_stt_ends_on_the_endpoint_and_cuts_the_wake_word(fake: FakeSoniox) -> None:
    config = {"language": "ru", "wake_words": ["Алиса"], "context_terms": ["свет"]}
    node = await started(SonioxStt, config)
    run = await run_node_once(node, {"audio": speech(2000)})  # the satellite would keep going
    assert run.error is None, run.error
    texts = [(t.text, t.final) for t in run.outputs["transcript"].items]
    assert texts[-1] == ("Включи свет", True)
    assert ("Включи", False) in texts  # partials, the wake word already cut
    assert run.outputs["utterance"].items[0].text == "Включи свет"
    # the input never ends (end="hang"): the turn finished on Soniox's endpoint
    assert fake.stt_audio >= 9600
    assert fake.auth == [f"Bearer {KEY}"]
    assert fake.stt_config["language_hints"] == ["ru"]
    assert fake.stt_config["context"] == {"terms": ["свет", "Алиса"]}
    (event,) = [e for e in run.trace.events if e.event == "soniox.stt"]
    assert event.attrs["ended_by"] == "end" and event.attrs["wake_word_cut"] is True


async def test_stt_tells_the_language_most_of_the_text_was_heard_in(fake: FakeSoniox) -> None:
    fake.languages = {"Алиса": "ru", "включи": "en", "свет": "en"}
    node = await started(SonioxStt, {"language": "ru"})
    run = await run_node_once(node, {"audio": speech(2000)})
    assert run.error is None, run.error
    assert fake.stt_config["enable_language_identification"] is True
    # the chosen language is only a hint: the other language must not be forced into it
    assert fake.stt_config["language_hints"] == ["ru"]
    assert fake.stt_config["language_hints_strict"] is False
    assert run.outputs["utterance"].items[0].language == "en"  # 10 letters to 5
    assert run.outputs["transcript"].items[-1].language == "en"
    (event,) = [e for e in run.trace.events if e.event == "soniox.stt"]
    assert event.attrs["language"] == "en"


async def test_stt_without_language_detection_keeps_the_chosen_language(
    fake: FakeSoniox,
) -> None:
    fake.languages = {"включи": "en", "свет": "en"}
    node = await started(SonioxStt, {"language": "ru", "detect_language": False})
    run = await run_node_once(node, {"audio": speech(2000)})
    assert run.error is None, run.error
    assert "enable_language_identification" not in fake.stt_config
    assert fake.stt_config["language_hints_strict"] is True
    assert run.outputs["utterance"].items[0].language == "ru"


async def test_stt_without_detected_language_tells_the_hint(fake: FakeSoniox) -> None:
    run = await run_node_once(
        await started(SonioxStt, {}), {"audio": speech(2000)}, context={"language": "ru-RU"}
    )
    assert run.error is None, run.error
    assert run.outputs["utterance"].items[0].language == "ru"


async def test_stt_finalizes_when_the_audio_ends(fake: FakeSoniox) -> None:
    fake.stt_script = "finalize"
    node = await started(SonioxStt, {})
    run = await run_node_once(node, {"audio": speech(300, end="end")})
    assert run.error is None, run.error
    assert run.outputs["utterance"].items[0].text == "Алиса включи свет"
    assert [e.attrs["ended_by"] for e in run.trace.events if e.event == "soniox.stt"] == [
        "finalize"
    ]


async def test_stt_reports_soniox_errors(fake: FakeSoniox) -> None:
    fake.stt_script = "error"
    run = await run_node_once(await started(SonioxStt, {}), {"audio": speech(200)})
    assert isinstance(run.error, SonioxError) and "bad key" in str(run.error)


async def test_tts_streams_speech(fake: FakeSoniox) -> None:
    node = await started(SonioxTts, {"voice": "Adrian", "sample_rate": 16000})
    deltas = [TextDelta("Hello"), TextDelta(", world")]
    run = await run_node_once(node, {"text": InputFeed(items=deltas, meta={}, end="end")})
    assert run.error is None, run.error
    out = run.outputs["speech"]
    assert out.header is not None and out.header.meta["sample_rate"] == 16000
    assert sum(len(c.pcm) for c in out.items) == 2 * 100 * len("Hello, world")
    assert fake.tts_configs[0] | {"stream_id": None} == {
        "stream_id": None,
        "model": "tts-rt-v1",
        "voice": "Adrian",
        "language": "en",
        "audio_format": "pcm_s16le",
        "sample_rate": 16000,
    }


@pytest.mark.parametrize(
    ("config", "meta", "spoken"),
    [
        ({}, {"language": "ru"}, "ru"),  # the agent's answer says its language
        ({}, {}, "en"),  # else the turn's
        ({"language": "en"}, {"language": "ru"}, "en"),  # a chosen language wins
        ({}, {"language": "de"}, "en"),  # a language the model lacks: its first
    ],
)
async def test_tts_speaks_the_language_of_the_text(
    fake: FakeSoniox, config: dict[str, Any], meta: dict[str, Any], spoken: str
) -> None:
    node = await started(SonioxTts, {"voice": "Adrian"} | config)
    feed = InputFeed(items=[TextDelta("Привет")], meta=meta, end="end")
    run = await run_node_once(node, {"text": feed}, context={"language": "en"})
    assert run.error is None, run.error
    assert fake.tts_configs[0]["language"] == spoken


async def test_tts_starts_a_new_stream_after_a_pause(fake: FakeSoniox) -> None:
    """Soniox ends a stream when text pauses (an agent calling a tool); the next text goes on."""
    fake.tts_timeout_first = True
    audio = bytearray()

    async def collect(pcm: bytes) -> None:
        audio.extend(pcm)

    synthesis = tts._Synthesis(KEY, {"model": "tts-rt-v1", "voice": "Maya"}, collect)
    try:
        await synthesis.connect()
        await synthesis.add_text("One. ")
        for _ in range(100):  # the pause: Soniox times the stream out
            if fake.tts_text and synthesis._terminated[synthesis._stream_id or ""].is_set():
                break
            await asyncio.sleep(0.01)
        await synthesis.add_text("Two.")
        await synthesis.finish()
    finally:
        await synthesis.close()
    assert synthesis.streams == 2 and len(fake.tts_configs) == 2
    assert len(audio) == 2 * 100 * len("One. Two.")


async def test_tts_rejects_an_unknown_voice(fake: FakeSoniox) -> None:
    with pytest.raises(SonioxError, match="no voice"):
        await started(SonioxTts, {"voice": "Nobody"})
    with pytest.raises(SonioxError, match="not_computed for model tts-rt-v1"):
        await started(SonioxTts, {"voice": OLD})
    with pytest.raises(SonioxError, match="no cloned voice"):
        await started(SonioxTts, {"voice": "11111111-2222-4333-8444-555555555555"})


async def test_tts_speaks_with_a_cloned_voice(fake: FakeSoniox) -> None:
    node = await started(SonioxTts, {"voice": MINE})
    run = await run_node_once(
        node, {"text": InputFeed(items=[TextDelta("Hi")], meta={}, end="end")}
    )
    assert run.error is None, run.error
    assert fake.tts_configs[0]["voice"] == MINE  # Soniox resolves the UUID to the clone


async def test_setting_choices_use_the_resolved_key(fake: FakeSoniox) -> None:
    resolved = {"api_key": KEY}
    voices = await SonioxTts.config_options("voice", resolved)
    assert [(v.id, v.name, v.description) for v in voices] == [
        (MINE, "Моя бабушка", "cloned"),  # every page of the project's voices, first
        (OLD, "Old", "cloned, not_computed for tts-rt-v1"),
        ("Maya", "Maya", "female"),
        ("Adrian", "Adrian", "male"),
    ]
    models = await SonioxStt.config_options("model", resolved)
    assert [m.id for m in models] == ["stt-rt-v5"]
    assert await SonioxTts.config_options("voice", CONFIG) == []  # the key is not set yet
    assert len(await SonioxStt.config_options("language", {})) > 50


async def test_conforms(fake: FakeSoniox) -> None:
    secrets = {"soniox": KEY}
    stt_sample = {"audio": speech(400, end="end")}
    tts_sample = {"text": InputFeed(items=[TextDelta("Hi.")], meta={}, end="end")}
    assert_conforms(await check_node(SonioxStt, config=CONFIG, secrets=secrets, sample=stt_sample))
    assert_conforms(await check_node(SonioxTts, config=CONFIG, secrets=secrets, sample=tts_sample))
