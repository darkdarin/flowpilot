"""End-to-end protocol tests over a real WebSocket."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, ClassVar

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.runtime import Deployment
from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    Transcript,
    TurnSession,
    Utterance,
)
from flowpilot_server.nodes import ClientSink, ClientSource, HaConversation, HaStt, HaTts
from flowpilot_server.protocol import PROTOCOL_VERSION, WS_PATH, pack_audio, unpack_audio
from flowpilot_server.server import FlowPilotServer

TOKEN = "test-token"
FMT = AudioFormat(sample_rate=16000)


class _Node:
    spec: ClassVar[NodeSpec]

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None


class FakeStt(_Node):
    """Recognizes the tag carried in the first audio chunk."""

    spec = NodeSpec(
        type="test.stt",
        version="1.0.0",
        title="Fake STT",
        category="speech",
        inputs={"audio": PortSpec("audio.pcm/1")},
        outputs={
            "transcript": PortSpec("text.transcript/1"),
            "utterance": PortSpec("text.utterance/1", kind="value"),
        },
        turn_timeout_s=5.0,
    )

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.input("audio")
        if await audio.header() is None:
            return
        chunks = [c.pcm async for c in audio]
        tag = chunks[0].rstrip(b"\x00").decode() if chunks else ""
        await turn.output("transcript").send(Transcript(f"heard {tag}", final=False))
        await turn.output("transcript").send(Transcript(f"heard {tag}", final=True))
        await turn.output("utterance").send(Utterance(f"heard {tag}", turn.context.language))


class FakeTts(_Node):
    spec = NodeSpec(
        type="test.tts",
        version="1.0.0",
        title="Fake TTS",
        category="speech",
        inputs={"text": PortSpec("text.delta/1")},
        outputs={"speech": PortSpec("audio.pcm/1")},
        turn_timeout_s=5.0,
    )

    async def run_turn(self, turn: TurnSession) -> None:
        text = turn.input("text")
        if await text.header() is None:
            return
        speech = turn.output("speech")
        await speech.open(FMT.as_meta())
        offset = 0
        async for delta in text:
            pcm = delta.text.encode()
            await speech.send(AudioChunk(pcm, offset))
            offset += len(pcm) // 2


GRAPH = {
    "schema_version": 1,
    "id": "main",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "test.stt"},
        {"id": "utt", "type": "text.first"},
        {"id": "agent", "type": "ha.conversation", "config": {"agent_id": "conversation.ha"}},
        {"id": "tts", "type": "test.tts"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "stt.transcript", "to": "out.transcript"},
        {"from": "stt.utterance", "to": "utt.primary"},
        {"from": "in.utterance", "to": "utt.fallback"},
        {"from": "utt.value", "to": "agent.utterance"},
        {"from": "agent.delta", "to": "out.delta"},
        {"from": "agent.delta", "to": "tts.text"},
        {"from": "tts.speech", "to": "out.speech"},
        {"from": "agent.response", "to": "out.response"},
    ],
}


class Client:
    """Minimal protocol client that records everything and serves ha.conversation.process."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.messages: list[dict[str, Any]] = []
        self.speech: dict[str, bytearray] = {}
        self.host_calls: list[dict[str, Any]] = []
        self.host_delay_s = 0.0
        self._stream_turn: dict[int, str] = {}
        self._cond = asyncio.Condition()
        self._streams = itertools.count(1)
        self._refs = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._host_in: dict[int, asyncio.Queue[bytes | None]] = {}
        self._out_streams = itertools.count(1000)
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                stream, pcm = unpack_audio(msg.data)
                if stream in self._host_in:
                    self._host_in[stream].put_nowait(pcm)
                else:
                    self.speech.setdefault(self._stream_turn[stream], bytearray()).extend(pcm)
                continue
            data = json.loads(msg.data)
            if data["type"] == "stream.start":
                self._stream_turn[data["stream"]] = data["turn_id"]
            if data["type"] == "stream.end" and data["stream"] in self._host_in:
                self._host_in.pop(data["stream"]).put_nowait(None)
            if data["type"] == "host.call":
                audio = data["args"].get("audio")
                if audio is not None:  # register before any audio frame can arrive
                    self._host_in[audio["stream"]] = asyncio.Queue()
                task = asyncio.create_task(self._serve(data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            async with self._cond:
                self.messages.append(data)
                self._cond.notify_all()

    async def _serve(self, call: dict[str, Any]) -> None:
        self.host_calls.append(call)
        await asyncio.sleep(self.host_delay_s)
        args = call["args"]
        if call["op"] == "ha.stt.process":
            queue = self._host_in[args["audio"]["stream"]]
            received = bytearray()
            while (pcm := await queue.get()) is not None:
                received.extend(pcm)
            tag = bytes(received).rstrip(b"\x00").decode()
            await self.ws.send_json(
                {"type": "host.result", "call": call["call"], "ok": True,
                 "result": {"text": f"heard {tag}"}}
            )  # fmt: skip
            return
        if call["op"] == "ha.tts.synthesize":
            stream = next(self._out_streams)
            fmt = {"encoding": "s16le", "sample_rate": 22050, "channels": 1}
            await self.ws.send_json(
                {"type": "stream.start", "call": call["call"], "stream": stream, "format": fmt}
            )
            await self.ws.send_bytes(pack_audio(stream, args["text"].encode()))
            await self.ws.send_json({"type": "stream.end", "stream": stream})
            await self.ws.send_json(
                {"type": "host.result", "call": call["call"], "ok": True, "result": {}}
            )
            return
        await self.ws.send_json(
            {
                "type": "host.result",
                "call": call["call"],
                "ok": True,
                "result": {"speech": f"echo: {args['text']}", "continue_conversation": False},
            }
        )

    async def wait_for(self, pred: Callable[[dict[str, Any]], bool], within: float = 5) -> Any:
        async with asyncio.timeout(within), self._cond:
            while True:
                for m in self.messages:
                    if pred(m):
                        return m
                await self._cond.wait()

    async def hello(self, host_ops: list[str] | None = None, token: str = TOKEN) -> Any:
        ops = ["ha.conversation.process"] if host_ops is None else host_ops
        await self.ws.send_json(
            {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "token": token,
                "client": {"name": "test"},
                "host_ops": ops,
            }
        )
        return await self.wait_for(lambda m: m["type"] in ("hello.ok", "error"))

    async def start(self, **body: Any) -> str:
        ref = str(next(self._refs))
        await self.ws.send_json({"type": "turn.start", "ref": ref, **body})
        msg = await self.wait_for(lambda m: m.get("ref") == ref)
        assert msg["type"] == "turn.started", msg
        turn_id: str = msg["turn_id"]
        return turn_id

    async def voice(self, tag: str, chunks: int = 10, **body: Any) -> str:
        stream = next(self._streams)
        audio = {"stream": stream, "format": FMT.as_meta()}
        turn_id = await self.start(input={"audio": audio}, language="en", **body)
        first = tag.encode().ljust(640, b"\x00")
        for i in range(chunks):
            await self.ws.send_bytes(pack_audio(stream, first if i == 0 else bytes(640)))
            await asyncio.sleep(0.002)
        await self.ws.send_json({"type": "stream.end", "stream": stream})
        return turn_id

    async def end(self, turn_id: str) -> Any:
        return await self.wait_for(lambda m: m["type"] == "turn.end" and m["turn_id"] == turn_id)

    def of(self, turn_id: str, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["type"] == kind and m.get("turn_id") == turn_id]


@pytest.fixture
async def server() -> AsyncIterator[TestServer]:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, HaConversation, FakeStt, FakeTts):
        registry.register(cls)
    vg = validate_graph(parse_graph(GRAPH), registry, grants=GrantPolicy())
    deployment = Deployment(vg, registry)
    await deployment.start()
    app = FlowPilotServer({"main": deployment}, token=TOKEN, default_graph="main").app()
    ts = TestServer(app)
    await ts.start_server()
    yield ts
    await ts.close()
    await deployment.stop()


@pytest.fixture
async def connect(server: TestServer) -> AsyncIterator[Callable[[], Any]]:
    session = aiohttp.ClientSession()
    clients: list[Client] = []

    async def make() -> Client:
        ws = await session.ws_connect(server.make_url(WS_PATH))
        client = Client(ws)
        clients.append(client)
        return client

    yield make
    for c in clients:
        await c.ws.close()
        await asyncio.gather(c.reader, return_exceptions=True)
    await session.close()


async def test_auth(connect: Any) -> None:
    c: Client = await connect()
    reply = await c.hello(token="wrong")
    assert reply == {"type": "error", "code": "auth", "message": "invalid token"}

    c2: Client = await connect()
    await c2.ws.send_json({"type": "turn.start", "input": {"utterance": {"text": "x"}}})
    assert (await c2.wait_for(lambda m: m["type"] == "error"))["code"] == "auth"


async def test_hello_reports_graphs(connect: Any) -> None:
    c: Client = await connect()
    reply = await c.hello()
    assert reply["type"] == "hello.ok"
    assert reply["graphs"] == ["main"] and reply["default_graph"] == "main"
    assert reply["missing_host_ops"] == {}

    c2: Client = await connect()
    reply = await c2.hello(host_ops=[])
    assert reply["missing_host_ops"] == {"main": ["ha.conversation.process"]}


async def test_text_turn(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()
    turn_id = await c.start(
        input={"utterance": {"text": "turn on the light"}},
        language="en",
        source={"kind": "ha.conversation", "device_id": "dev1", "satellite_id": "sat1"},
        external={"ha.conversation_id": "conv-1"},
    )
    end = await c.end(turn_id)
    assert end["status"] == "ok", end
    call = c.host_calls[0]
    assert call["op"] == "ha.conversation.process" and call["turn_id"] == turn_id
    assert call["args"]["text"] == "turn on the light"
    assert call["args"]["conversation_id"] == "conv-1"
    assert call["args"]["device_id"] == "dev1" and call["args"]["satellite_id"] == "sat1"
    assert call["args"]["agent_id"] == "conversation.ha"
    response = c.of(turn_id, "turn.response")[0]["response"]
    assert response["text"] == "echo: turn on the light"
    assert [d["text"] for d in c.of(turn_id, "turn.delta")] == ["echo: turn on the light"]
    assert bytes(c.speech[turn_id]) == b"echo: turn on the light"
    assert c.of(turn_id, "turn.transcript") == []


async def test_voice_turn_streams_both_ways(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()
    turn_id = await c.voice("alice")
    end = await c.end(turn_id)
    assert end["status"] == "ok", end
    transcripts = c.of(turn_id, "turn.transcript")
    assert [(t["text"], t["final"]) for t in transcripts] == [
        ("heard alice", False),
        ("heard alice", True),
    ]
    assert c.of(turn_id, "turn.response")[0]["response"]["text"] == "echo: heard alice"
    assert bytes(c.speech[turn_id]) == b"echo: heard alice"
    starts = c.of(turn_id, "stream.start")
    assert starts[0]["kind"] == "speech" and starts[0]["format"]["sample_rate"] == 16000


async def test_concurrent_turns_on_one_connection_never_mix(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()
    c.host_delay_s = 0.02
    names = ["alice", "bob", "carol"]
    turn_ids = await asyncio.gather(*(c.voice(n) for n in names))
    await asyncio.gather(*(c.end(t) for t in turn_ids))
    for name, turn_id in zip(names, turn_ids, strict=True):
        assert c.of(turn_id, "turn.response")[0]["response"]["text"] == f"echo: heard {name}"
        assert bytes(c.speech[turn_id]) == f"echo: heard {name}".encode()


async def test_missing_host_op_fails_closed(connect: Any) -> None:
    c: Client = await connect()
    await c.hello(host_ops=[])
    turn_id = await c.start(input={"utterance": {"text": "hi"}})
    end = await c.end(turn_id)
    assert end["status"] == "degraded"
    assert end["nodes"]["agent"] == "error"
    assert c.of(turn_id, "turn.response") == []


async def test_cancel_turn(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()
    turn_id = await c.start(input={"audio": {"stream": 99, "format": FMT.as_meta()}})
    await c.ws.send_json({"type": "turn.cancel", "turn_id": turn_id})
    end = await c.end(turn_id)
    assert end["status"] == "cancelled"


async def test_session_follows_ha_conversation_id(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()

    async def session_of(conv: str) -> str:
        ref = f"s-{conv}-{len(c.messages)}"
        await c.ws.send_json(
            {
                "type": "turn.start",
                "ref": ref,
                "input": {"utterance": {"text": "x"}},
                "external": {"ha.conversation_id": conv},
            }
        )
        started = await c.wait_for(lambda m: m.get("ref") == ref)
        await c.end(started["turn_id"])
        session_id: str = started["session_id"]
        return session_id

    a1, a2, b = await session_of("a"), await session_of("a"), await session_of("b")
    assert a1 == a2 and a1 != b


async def test_protocol_errors(connect: Any) -> None:
    c: Client = await connect()
    await c.hello()
    await c.ws.send_json({"type": "turn.start", "ref": "r1", "graph": "nope", "input": {"x": 1}})
    err = await c.wait_for(lambda m: m.get("ref") == "r1")
    assert err["type"] == "error" and err["code"] == "unknown_graph"
    await c.ws.send_bytes(pack_audio(12345, b"\x00\x00"))
    err = await c.wait_for(lambda m: m.get("code") == "unknown_stream")
    assert err["type"] == "error"


async def test_shutdown_closes_client_connections() -> None:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, HaConversation, FakeStt, FakeTts):
        registry.register(cls)
    deployment = Deployment(validate_graph(parse_graph(GRAPH), registry), registry)
    await deployment.start()
    ts = TestServer(FlowPilotServer({"main": deployment}, token=TOKEN, default_graph="main").app())
    await ts.start_server()
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(ts.make_url(WS_PATH))
        await ws.send_json({"type": "hello", "protocol": PROTOCOL_VERSION, "token": TOKEN})
        await ws.receive_json()
        started = asyncio.get_running_loop().time()
        await ts.close()
        assert asyncio.get_running_loop().time() - started < 5
        msg = await ws.receive()
        assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await deployment.stop()


HA_GRAPH = {
    "schema_version": 1,
    "id": "ha",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "ha.stt", "config": {"entity_id": "stt.cloud"}},
        {"id": "utt", "type": "text.first"},
        {"id": "agent", "type": "ha.conversation"},
        {"id": "tts", "type": "ha.tts", "config": {"engine": "tts.cloud"}},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "stt.transcript", "to": "out.transcript"},
        {"from": "stt.utterance", "to": "utt.primary"},
        {"from": "in.utterance", "to": "utt.fallback"},
        {"from": "utt.value", "to": "agent.utterance"},
        {"from": "agent.delta", "to": "tts.text"},
        {"from": "tts.speech", "to": "out.speech"},
        {"from": "agent.response", "to": "out.response"},
    ],
}


async def test_ha_stt_and_tts_stream_audio_through_host_operations() -> None:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, HaConversation, HaStt, HaTts):
        registry.register(cls)
    deployment = Deployment(validate_graph(parse_graph(HA_GRAPH), registry), registry)
    await deployment.start()
    ts = TestServer(FlowPilotServer({"ha": deployment}, token=TOKEN, default_graph="ha").app())
    await ts.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            c = Client(await session.ws_connect(ts.make_url(WS_PATH)))
            await c.hello(
                host_ops=["ha.conversation.process", "ha.stt.process", "ha.tts.synthesize"]
            )
            turn_id = await c.voice("alice")
            end = await c.end(turn_id)
            assert end["status"] == "ok", end
            called = [call["op"] for call in c.host_calls]
            assert called == ["ha.stt.process", "ha.conversation.process", "ha.tts.synthesize"]
            assert c.host_calls[0]["args"]["entity_id"] == "stt.cloud"
            assert c.host_calls[2]["args"]["engine"] == "tts.cloud"
            transcript = c.of(turn_id, "turn.transcript")[0]
            assert (transcript["text"], transcript["final"]) == ("heard alice", True)
            assert bytes(c.speech[turn_id]) == b"echo: heard alice"
            start = c.of(turn_id, "stream.start")[0]
            assert start["format"]["sample_rate"] == 22050
            await c.ws.close()
            await asyncio.gather(c.reader, return_exceptions=True)
    finally:
        await ts.close()
        await deployment.stop()
