"""Live debug's journal: what every node passed on in a watched turn."""

from __future__ import annotations

import json
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from flowpilot_core import NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.runtime import Deployment, ListTraceSink
from flowpilot_sdk import (
    AudioChunk,
    AudioFormat,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    Response,
    SessionInfo,
    SourceInfo,
    TextDelta,
    Transcript,
    TurnContext,
    TurnSession,
    Utterance,
    new_ulid,
)
from flowpilot_server.captures import KEEP_TURNS, TurnCapture, list_recordings, prune

RATE = 16000


class Source:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="t.source",
        title="source",
        category="input",
        version="1.0.0",
        outputs={
            "audio": PortSpec("audio.pcm/1"),
            "transcript": PortSpec("text.transcript/1"),
            "utterance": PortSpec("text.utterance/1", kind="value"),
            "delta": PortSpec("text.delta/1"),
            "response": PortSpec("agent.response/1", kind="value"),
            "silent": PortSpec("text.delta/1"),
        },
    )

    async def start(self, ctx: NodeContext) -> None: ...

    async def stop(self) -> None: ...

    async def run_turn(self, turn: TurnSession) -> None:
        audio = turn.output("audio")
        await audio.open(AudioFormat(sample_rate=RATE).as_meta())
        for i in range(5):  # half a second
            await audio.send(AudioChunk(bytes(RATE // 10 * 2), i * RATE // 10))
        await audio.end()
        transcript = turn.output("transcript")
        for text, final in (("вклю", False), ("включи свет", False), ("Включи свет.", True)):
            await transcript.send(Transcript(text, final=final))
        await transcript.end()
        await turn.output("utterance").send(Utterance("Включи свет.", "ru"))
        delta = turn.output("delta")
        for piece in ("Включаю", " свет."):
            await delta.send(TextDelta(piece))
        await delta.end()
        await turn.output("response").send(
            Response(text="Включаю свет.", data={"acknowledge": "on"})
        )


class Sink:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="t.sink",
        title="sink",
        category="output",
        version="1.0.0",
        inputs={"speech": PortSpec("audio.pcm/1", required=False)},
    )

    async def start(self, ctx: NodeContext) -> None: ...

    async def stop(self) -> None: ...

    async def run_turn(self, turn: TurnSession) -> None:
        async for _ in turn.input("speech"):
            pass


GRAPH = {
    "schema_version": 2,
    "id": "main",
    "name": "g",
    "nodes": [
        {"id": "in", "type": "t.source"},
        {"id": "pass", "type": "text.first"},
        {"id": "out", "type": "t.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "pass.primary"},
        {"from": "pass.value", "to": "out.speech"},
    ],
}


async def run(tmp_path: Path, *, watched: bool) -> list[Any]:
    reg = register_builtin(NodeRegistry())
    for cls in (Source, Sink):
        reg.register(cls)
    trace = ListTraceSink()
    deployment = Deployment(
        validate_graph(parse_graph(GRAPH), reg),
        reg,
        trace=trace,
        taps_active=lambda: watched,
        output_tap=lambda context: TurnCapture(trace, context, "main", tmp_path),
    )
    await deployment.start()
    session = SessionInfo(session_id=new_ulid(), created_at=datetime.now(UTC), turn_index=0)
    context = TurnContext(
        turn_id=new_ulid(),
        session_id=session.session_id,
        started_at=datetime.now(UTC),
        deadline_mono_ns=time.monotonic_ns() + 5_000_000_000,
        source=SourceInfo(kind="test", adapter_id="test"),
        trace_id=new_ulid(),
        initial_principal=Principal.unknown(),
    )
    await deployment.run_turn(context, session)
    await deployment.stop()
    return [e for e in trace.events if e.event == "port.data"]


async def test_a_watched_turn_reports_what_every_output_passed_on(tmp_path: Path) -> None:
    events = await run(tmp_path, watched=True)
    data = {f"{e.node_id}.{e.attrs['port']}": e.attrs for e in events}
    # text as text: recognized speech as its final text, a stream joined, an utterance
    assert (
        data["in.transcript"]["text"] == "Включи свет." and data["in.transcript"]["partials"] == 2
    )
    assert data["in.delta"]["text"] == "Включаю свет." and data["in.delta"]["items"] == 2
    assert data["in.utterance"]["text"] == "Включи свет."
    # anything else as JSON
    answer = json.loads(data["in.response"]["json"])
    assert answer["text"] == "Включаю свет." and answer["data"] == {"acknowledge": "on"}
    # audio as a recording kept on this machine
    audio = data["in.audio"]
    assert audio["seconds"] == 0.5
    assert set(audio) == {"port", "type", "items", "state", "file", "seconds"}  # no sound in it
    with wave.open(str(tmp_path / audio["file"]), "rb") as wav:
        assert wav.getframerate() == RATE and wav.getnframes() == RATE // 2
    [recording] = list_recordings(tmp_path, "main")
    assert (recording.node, recording.port) == ("in", "audio")
    # First present passes the same audio on: marked as such, not recorded twice
    assert data["pass.value"]["same"] == "in.audio" and "file" not in data["pass.value"]
    assert "in.silent" not in data  # nothing went through it


class BrokenTap:
    def opened(self, *args: Any) -> None: ...

    def item(self, *args: Any) -> None:
        raise RuntimeError("tap bug")

    def ended(self, *args: Any) -> None: ...

    async def close(self) -> None: ...


async def test_a_broken_tap_loses_debug_data_not_the_turn(tmp_path: Path) -> None:
    reg = register_builtin(NodeRegistry())
    for cls in (Source, Sink):
        reg.register(cls)
    deployment = Deployment(
        validate_graph(parse_graph(GRAPH), reg),
        reg,
        taps_active=lambda: True,
        output_tap=lambda context: BrokenTap(),
    )
    await deployment.start()
    session = SessionInfo(session_id=new_ulid(), created_at=datetime.now(UTC), turn_index=0)
    context = TurnContext(
        turn_id=new_ulid(),
        session_id=session.session_id,
        started_at=datetime.now(UTC),
        deadline_mono_ns=time.monotonic_ns() + 5_000_000_000,
        source=SourceInfo(kind="test", adapter_id="test"),
        trace_id=new_ulid(),
        initial_principal=Principal.unknown(),
    )
    result = await deployment.run_turn(context, session)
    await deployment.stop()
    assert result.status.value == "ok"


async def test_nothing_is_captured_while_nobody_watches(tmp_path: Path) -> None:
    assert await run(tmp_path, watched=False) == []
    assert list_recordings(tmp_path) == []


def test_recordings_of_the_last_turns_are_kept(tmp_path: Path) -> None:
    for i in range(KEEP_TURNS + 3):
        for port in ("audio", "speech"):
            name = f"20261113T12{i:04d}Z.main.n-{port}.T{i:03d}.wav"
            (tmp_path / name).write_bytes(b"")
    (tmp_path / "20261113T110000Z.other.n-audio.X.wav").write_bytes(b"")
    prune(tmp_path, "main", KEEP_TURNS)
    turns = {p.name.split(".")[3] for p in tmp_path.glob("*.main.*.wav")}
    assert turns == {f"T{i:03d}" for i in range(3, KEEP_TURNS + 3)}
    assert (tmp_path / "20261113T110000Z.other.n-audio.X.wav").exists()  # another graph's
