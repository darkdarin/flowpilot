"""The voice agent (``llm.voice``) on a fake OpenAI Realtime API."""

from __future__ import annotations

import base64
import json
from array import array
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_core.runtime.state import MemorySessionStore
from flowpilot_llm import VoiceAgent
from flowpilot_llm.voice import RATE, Resampler, _models_url, _session_url
from flowpilot_sdk import AudioChunk, AudioFormat
from flowpilot_server.memory_store import MemoryStore

KEY = "sk-test"
ANSWER_PCM = array("h", [1000, -1000] * 2400).tobytes()  # 0.2 s at 24 kHz


class FakeRealtime:
    """Hears speech after ``speech_after`` bytes, ends the command after ``commit_after`` (its
    voice activity detection), answers with audio and words; a scripted tool call first."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.query: dict[str, str] = {}
        self.auth = ""
        self.speech_after = 9600
        self.commit_after: int | None = 24000
        self.calls: list[dict[str, Any]] = []  # tool calls of the first response
        self.answer = "Сейчас двенадцать часов."
        self.heard = "который час"
        self.fail_with: dict[str, Any] | None = None

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == kind]

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        self.query = dict(request.query)
        self.auth = request.headers.get("Authorization", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        buffered = 0
        spoke = committed = False
        responses = 0

        async def send(event: dict[str, Any]) -> None:
            await ws.send_str(json.dumps(event, ensure_ascii=False))

        async def commit() -> None:
            nonlocal committed, buffered
            committed = True
            buffered = 0
            await send({"type": "input_audio_buffer.committed", "item_id": "u1"})
            await send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "u1",
                    "transcript": self.heard,
                }
            )

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            event = json.loads(msg.data)
            self.events.append(event)
            kind = event["type"]
            if kind == "session.update":
                await send({"type": "session.updated", "session": event["session"]})
            elif kind == "input_audio_buffer.append":
                buffered += len(base64.b64decode(event["audio"]))
                if not spoke and buffered >= self.speech_after:
                    spoke = True
                    await send({"type": "input_audio_buffer.speech_started", "item_id": "u1"})
                if (
                    not committed
                    and self.commit_after is not None
                    and buffered >= self.commit_after
                ):
                    await send({"type": "input_audio_buffer.speech_stopped", "item_id": "u1"})
                    await commit()
            elif kind == "input_audio_buffer.commit":
                if committed or not buffered:
                    error = {"code": "input_audio_buffer_commit_empty", "message": "empty"}
                    await send({"type": "error", "error": error})
                else:
                    await commit()
            elif kind == "response.create":
                responses += 1
                if self.fail_with is not None:
                    await send({"type": "error", "error": self.fail_with})
                    continue
                if responses == 1 and self.calls:
                    output = [
                        {
                            "type": "function_call",
                            "status": "completed",
                            "name": c["name"],
                            "call_id": f"call{i}",
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        }
                        for i, c in enumerate(self.calls)
                    ]
                    await send(
                        {
                            "type": "response.done",
                            "response": {"status": "completed", "output": output, "usage": {}},
                        }
                    )
                    continue
                half = len(ANSWER_PCM) // 2
                for part in (ANSWER_PCM[:half], ANSWER_PCM[half:]):
                    delta = base64.b64encode(part).decode()
                    await send({"type": "response.output_audio.delta", "delta": delta})
                for word in (self.answer[:7], self.answer[7:]):
                    await send({"type": "response.output_audio_transcript.delta", "delta": word})
                usage = {"input_tokens": 120, "output_tokens": 40}
                done = {"status": "completed", "output": [{"type": "message"}], "usage": usage}
                await send({"type": "response.done", "response": done})
        return ws


@pytest.fixture
async def realtime() -> AsyncIterator[tuple[FakeRealtime, str]]:
    fake = FakeRealtime()
    app = web.Application()
    app.router.add_get("/v1/realtime", fake.handler)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("/v1/realtime")).replace("http", "ws", 1)
    await server.close()


def speech(seconds: float = 1.0, rate: int = 16000, delay_s: float = 0.005) -> InputFeed:
    samples = int(seconds * rate)
    pcm = array("h", [(i * 37) % 2000 - 1000 for i in range(samples)]).tobytes()
    step = rate // 10 * 2  # 100 ms chunks
    chunks = [AudioChunk(pcm[i : i + step], i // 2) for i in range(0, len(pcm), step)]
    meta = AudioFormat(sample_rate=rate).as_meta()
    return InputFeed(items=chunks, meta=meta, end="end", delay_s=delay_s)


async def hear(
    url: str, feed: InputFeed, *, store: MemoryStore | None = None, **config: Any
) -> Any:
    node = VoiceAgent()
    cfg = {"url": url, "api_key": {"secret": "k"}, "system_prompt": "Ты — Степа."} | config
    ctx = RuntimeNodeContext("voice", cfg, {"k": KEY}, frozenset({"k"}), memory=store)
    await node.start(ctx)
    try:
        return await run_node_once(node, {"audio": feed}, context={"language": "ru"})
    finally:
        await node.stop()


async def test_hears_and_answers_in_its_own_voice(realtime: tuple[FakeRealtime, str]) -> None:
    fake, url = realtime
    run = await hear(url, speech())
    assert run.error is None, run.error
    assert fake.auth == f"Bearer {KEY}"
    assert fake.query == {"model": "gpt-realtime-2.1-mini"}
    [update] = fake.of("session.update")
    session = update["session"]
    assert session["instructions"].startswith("Ты — Степа.")
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": RATE}
    assert session["audio"]["input"]["turn_detection"]["create_response"] is False
    assert session["audio"]["output"]["voice"] == "marin"
    assert session["audio"]["input"]["transcription"]["language"] == "ru"
    # 16 kHz in, 24 kHz to the model; the model's end of the command stopped the audio
    sent = sum(len(base64.b64decode(e["audio"])) for e in fake.of("input_audio_buffer.append"))
    assert 24000 <= sent < 48000
    assert len(fake.of("response.create")) == 1  # once, by the agent
    assert run.outputs["speech"].header.meta["sample_rate"] == RATE
    assert b"".join(c.pcm for c in run.outputs["speech"].items) == ANSWER_PCM
    assert "".join(d.text for d in run.outputs["delta"].items) == fake.answer
    assert [t.text for t in run.outputs["transcript"].items] == ["который час"]
    [response] = run.outputs["response"].items
    assert (response.text, response.language) == (fake.answer, "ru")
    [event] = [e for e in run.trace.events if e.event == "llm.voice"]
    assert event.attrs["ended_by"] == "model"
    assert (event.attrs["input_tokens"], event.attrs["output_tokens"]) == (120, 40)


async def test_the_end_of_the_audio_asks_for_the_answer(
    realtime: tuple[FakeRealtime, str],
) -> None:
    fake, url = realtime
    fake.commit_after = None  # the model does not hear the end; the satellite does
    run = await hear(url, speech(0.6))
    assert run.error is None, run.error
    assert len(fake.of("input_audio_buffer.commit")) == 1
    assert run.outputs["response"].items[0].text == fake.answer
    [event] = [e for e in run.trace.events if e.event == "llm.voice"]
    assert event.attrs["ended_by"] == "stream"


async def test_silence_is_no_command(realtime: tuple[FakeRealtime, str]) -> None:
    fake, url = realtime
    fake.speech_after = 10**9
    fake.commit_after = None
    run = await hear(url, speech(0.5))
    assert run.error is None, run.error
    assert fake.of("input_audio_buffer.commit") == []
    assert fake.of("response.create") == []
    assert run.outputs["response"].items == []


async def test_tools_run_between_responses(realtime: tuple[FakeRealtime, str]) -> None:
    fake, url = realtime
    memory = MemoryStore(None)
    fake.calls = [{"name": "remember", "arguments": {"fact": "Кот — Барсик.", "about": "home"}}]
    run = await hear(url, speech(), store=memory)
    assert run.error is None, run.error
    tools = fake.of("session.update")[0]["session"]["tools"]
    assert {"type", "name", "description", "parameters"} <= set(tools[0])
    assert "remember" in [t["name"] for t in tools]
    [fact] = memory.all()
    assert fact.text == "Кот — Барсик."
    [output] = fake.of("conversation.item.create")
    assert output["item"] == {
        "type": "function_call_output",
        "call_id": "call0",
        "output": "remembered for the home",
    }
    assert len(fake.of("response.create")) == 2
    assert run.outputs["response"].items[0].text == fake.answer


async def test_the_dialogue_goes_on_in_the_next_turn(realtime: tuple[FakeRealtime, str]) -> None:
    fake, url = realtime
    sessions = MemorySessionStore()
    node = VoiceAgent()
    cfg = {"url": url, "api_key": {"secret": "k"}}
    await node.start(RuntimeNodeContext("voice", cfg, {"k": KEY}, frozenset({"k"})))
    try:
        for _ in range(2):
            run = await run_node_once(node, {"audio": speech()}, sessions=sessions, session_id="s1")
            assert run.error is None, run.error
    finally:
        await node.stop()
    second = fake.of("session.update")[1]["session"]["instructions"]
    assert f"User: который час\nYou: {fake.answer}" in second


async def test_a_refusal_fails_the_turn(realtime: tuple[FakeRealtime, str]) -> None:
    fake, url = realtime
    fake.fail_with = {"code": "invalid_api_key", "message": "Incorrect API key provided"}
    run = await hear(url, speech())
    assert "Incorrect API key provided" in str(run.error)


async def test_conforms(realtime: tuple[FakeRealtime, str]) -> None:
    _, url = realtime
    report = await check_node(
        VoiceAgent,
        config={"url": url, "api_key": {"secret": "k"}},
        secrets={"k": KEY},
        sample={"audio": speech()},
    )
    assert_conforms(report)


def test_resampling_keeps_its_place_between_chunks() -> None:
    ramp = array("h", range(0, 3200, 2)).tobytes()  # 1600 samples
    whole = Resampler(16000, 24000).convert(ramp)
    pieces = Resampler(16000, 24000)
    parts = b"".join(pieces.convert(ramp[i : i + 202]) for i in range(0, len(ramp), 202))
    assert parts == whole
    out = array("h", whole)
    assert abs(len(out) - 2400) <= 2
    assert all(b >= a for a, b in zip(out, out[1:], strict=False))  # still a ramp
    assert Resampler(24000, 24000).convert(ramp) == ramp
    stereo = array("h", [100, 300] * 10).tobytes()
    assert set(array("h", Resampler(24000, 24000, channels=2).convert(stereo))) == {200}


def test_addresses() -> None:
    assert _models_url("wss://api.openai.com/v1/realtime") == "https://api.openai.com/v1/models"
    assert _models_url("wss://ai.api.cloud.yandex.net/v1/realtime/openai?model=x") is None
    assert _session_url("wss://h/v1/realtime", "m") == "wss://h/v1/realtime?model=m"
    assert _session_url("wss://h/rt?model=gpt://f/x", "m") == "wss://h/rt?model=gpt://f/x"
