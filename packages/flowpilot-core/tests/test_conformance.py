"""The conformance kit accepts well-behaved nodes and catches each kind of misbehaviour."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from flowpilot_core.conformance import (
    ConformanceReport,
    InputFeed,
    assert_conforms,
    check_node,
)
from flowpilot_sdk import (
    AudioChunk,
    Node,
    NodeContext,
    NodeSpec,
    PortSpec,
    Principal,
    Response,
    SpeakerMatch,
    SpeakerStatus,
    TextDelta,
    Transcript,
    TurnContext,
    TurnSession,
    Utterance,
)

from .runtime_nodes import (
    FMT,
    Agent,
    Feed,
    First,
    Resolve,
    Sink,
    Source,
    Speaker,
    Stt,
    Tts,
    tagged_audio,
)

AUDIO = InputFeed(
    items=[AudioChunk(pcm, i * 320) for i, pcm in enumerate(tagged_audio("alice", 10))],
    meta=FMT.as_meta(),
)


def _prepare_source(node: Node, ctx: TurnContext) -> None:
    assert isinstance(node, Source)
    node.feeds[ctx.turn_id] = Feed(audio=tagged_audio("alice", 5), text="hi")


GOOD: list[tuple[type[Node], dict[str, Any]]] = [
    (Source, {"prepare": _prepare_source}),
    (Stt, {"config": {"uri": "tcp://x:1"}, "sample": {"audio": AUDIO}}),
    (Speaker, {"config": {"model": "m"}, "sample": {"audio": AUDIO}}),
    (
        Resolve,
        {"sample": {"speaker": InputFeed([SpeakerMatch(SpeakerStatus.MATCHED, "alice")])}},
    ),
    (First, {"sample": {"primary": InputFeed([Utterance("hi")])}}),
    (
        Agent,
        {
            "config": {"agent_id": "a", "token_delay_s": 0.001},
            "sample": {
                "principal": InputFeed([Principal.unknown()]),
                "utterance": InputFeed([Utterance("hello there")]),
            },
        },
    ),
    (Tts, {"config": {"uri": "tcp://x:1"}, "sample": {"text": InputFeed([TextDelta("hi")])}}),
    (
        Sink,
        {
            "sample": {
                "transcript": InputFeed([Transcript("hi", final=True)]),
                "response": InputFeed([Response("hi")]),
                "speech": InputFeed([AudioChunk(b"\x00\x00", 0)], meta=FMT.as_meta()),
            }
        },
    ),
]


@pytest.mark.parametrize(("node_cls", "kwargs"), GOOD, ids=lambda v: getattr(v, "__name__", ""))
async def test_well_behaved_nodes_conform(node_cls: type[Node], kwargs: dict[str, Any]) -> None:
    report = await check_node(node_cls, **kwargs)
    assert_conforms(report)


async def test_missing_timeout_is_only_a_warning() -> None:
    report = await check_node(Stt, config={"uri": "tcp://x:1"}, sample={"audio": AUDIO})
    assert report.ok
    assert any(w.startswith("W1") for w in report.warnings)
    assert any(w.startswith("C4") for w in report.warnings)  # Stt raises on a failed input


# --- misbehaving nodes ----------------------------------------------------------------------------

TEXT_IN = {"text": PortSpec("text.utterance/1", kind="value", required=False)}
TEXT_OUT = {"text": PortSpec("text.utterance/1", kind="value")}


def spec(name: str, **kwargs: Any) -> NodeSpec:
    base: dict[str, Any] = {
        "type": f"bad.{name}",
        "version": "1.0.0",
        "title": name,
        "category": "logic",
        "inputs": TEXT_IN,
        "outputs": TEXT_OUT,
    }
    return NodeSpec(**(base | kwargs))


class Base:
    spec: ClassVar[NodeSpec]

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        value = await turn.input("text").value()
        if value is not None:
            await turn.output("text").send(value)


class CrashOnAbsent(Base):
    spec = spec("crash_on_absent")

    async def run_turn(self, turn: TurnSession) -> None:
        if await turn.input("text").value() is None:
            raise RuntimeError("no input")


class Hangs(Base):
    spec = spec("hangs", turn_timeout_s=0.2)

    async def run_turn(self, turn: TurnSession) -> None:
        await asyncio.Event().wait()


class SwallowsCancel(Base):
    spec = spec("swallows_cancel", inputs={"text": PortSpec("text.utterance/1")})

    async def run_turn(self, turn: TurnSession) -> None:
        try:
            async for _ in turn.input("text"):
                pass
        except asyncio.CancelledError:
            return


class IgnoresCancel(Base):
    spec = spec("ignores_cancel", inputs={"text": PortSpec("text.utterance/1")})

    async def run_turn(self, turn: TurnSession) -> None:
        try:
            async for _ in turn.input("text"):
                pass
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)
            raise


class LeaksTask(Base):
    spec = spec("leaks_task")

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    async def run_turn(self, turn: TurnSession) -> None:
        self._tasks.append(asyncio.create_task(asyncio.sleep(30)))


class WrongType(Base):
    spec = spec("wrong_type")

    async def run_turn(self, turn: TurnSession) -> None:
        if await turn.input("text").value() is not None:
            await turn.output("text").send(Transcript("x", final=True))


class BadSchema(Base):
    spec = spec("bad_schema", config_schema={"type": "nonsense"})


class BadStart(Base):
    spec = spec("bad_start")

    async def start(self, ctx: NodeContext) -> None:
        raise RuntimeError("cannot start")


TEXT_SAMPLE = {"text": InputFeed([Utterance("hi")])}

BAD: list[tuple[type[Node], str]] = [
    (CrashOnAbsent, "C3"),
    (Hangs, "C3"),
    (SwallowsCancel, "C6"),
    (IgnoresCancel, "C6"),
    (LeaksTask, "C7"),
    (WrongType, "C5"),
    (BadSchema, "C1"),
    (BadStart, "C2"),
]


@pytest.mark.parametrize(("node_cls", "code"), BAD, ids=lambda v: getattr(v, "__name__", v))
async def test_misbehaviour_is_caught(node_cls: type[Node], code: str) -> None:
    report = await check_node(node_cls, sample=TEXT_SAMPLE)
    assert not report.ok
    assert any(v.startswith(code) for v in report.violations), str(report)
    with pytest.raises(AssertionError, match=code):
        assert_conforms(report)


def test_report_str() -> None:
    r = ConformanceReport("x.y", violations=["C3 boom"], warnings=["W1 meh"])
    assert str(r) == "x.y: FAILED\n  violation C3 boom\n  warning   W1 meh"
