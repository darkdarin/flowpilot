"""Turn execution on the example graph."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from flowpilot_core import GrantPolicy, parse_graph, validate_graph
from flowpilot_core.runtime import (
    Deployment,
    ListTraceSink,
    NodeStatus,
    RuntimeNodeContext,
    TurnStatus,
)
from flowpilot_sdk import Principal, SessionInfo, SourceInfo, TurnContext, new_ulid

from .runtime_nodes import Feed, Sink, Source, behaviour_registry, tagged_audio


def make_turn(session: SessionInfo, deadline_s: float = 5.0) -> TurnContext:
    return TurnContext(
        turn_id=new_ulid(),
        session_id=session.session_id,
        started_at=datetime.now(UTC),
        deadline_mono_ns=time.monotonic_ns() + int(deadline_s * 1e9),
        source=SourceInfo(kind="test", adapter_id="test"),
        trace_id=new_ulid(),
        initial_principal=Principal.unknown(),
    )


def new_session(index: int = 0, session_id: str | None = None) -> SessionInfo:
    return SessionInfo(
        session_id=session_id or new_ulid(), created_at=datetime.now(UTC), turn_index=index
    )


class Harness:
    def __init__(self, deployment: Deployment, trace: ListTraceSink) -> None:
        self.d = deployment
        self.trace = trace

    @property
    def source(self) -> Source:
        return cast(Source, self.d.instance("in"))

    @property
    def sink(self) -> Sink:
        return cast(Sink, self.d.instance("out"))

    async def turn(
        self, feed: Feed, *, session: SessionInfo | None = None, deadline_s: float = 5.0
    ) -> tuple[Any, dict[str, Any]]:
        ctx = make_turn(session or new_session(), deadline_s)
        self.source.feeds[ctx.turn_id] = feed
        result = await self.d.run_turn(ctx, session or new_session())
        return result, self.sink.results.get(ctx.turn_id, {})


@pytest.fixture
async def harness_factory(example: dict[str, Any], grants: GrantPolicy) -> AsyncIterator[Any]:
    started: list[Deployment] = []

    async def make(mutate: Any = None) -> Harness:
        data = copy.deepcopy(example)
        if mutate:
            mutate(data)
        registry = behaviour_registry()
        vg = validate_graph(parse_graph(data), registry, grants=grants)
        trace = ListTraceSink()
        d = Deployment(vg, registry, trace=trace)
        await d.start()
        started.append(d)
        return Harness(d, trace)

    yield make
    for d in started:
        await d.stop()


async def test_voice_turn(harness_factory: Any) -> None:
    h = await harness_factory()
    result, out = await h.turn(Feed(audio=tagged_audio("alice")))
    assert result.status is TurnStatus.OK
    assert out["finals"] == ["heard alice"]
    assert out["response"].text == "alice: heard alice"
    assert {e.node_id for e in h.trace.named("node.end")} == {
        "in",
        "stt",
        "spk",
        "who",
        "utt",
        "agent",
        "tts",
        "out",
    }
    assert h.trace.named("port.first_item", "stt")
    assert h.trace.named("speaker.window", "spk")[0].attrs == {"chunks": 1}
    assert out["speech"] == b"alice: heard alice"


async def test_pipeline_streams_without_waiting_for_nodes(harness_factory: Any) -> None:
    """TTS audio starts while the agent is still generating; audio while STT still listens."""
    h: Harness = await harness_factory()
    result, out = await h.turn(Feed(audio=tagged_audio("alice"), chunk_delay_s=0.005))
    assert result.status is TurnStatus.OK

    def first_item(node: str, port: str) -> int:
        events = [e for e in h.trace.named("port.first_item", node) if e.attrs["port"] == port]
        return events[0].t_mono_ns

    def node_end(node: str) -> int:
        return h.trace.named("node.end", node)[0].t_mono_ns

    # Speech reaches the sink before the agent has finished its answer.
    assert first_item("tts", "speech") < first_item("agent", "response")
    assert first_item("tts", "speech") < node_end("agent")
    # Partial transcripts flow while the source is still sending audio.
    assert first_item("stt", "transcript") < node_end("in")
    assert out["speech"] == b"alice: heard alice"


async def test_text_turn_has_no_voice_identity(harness_factory: Any) -> None:
    h = await harness_factory()
    result, out = await h.turn(Feed(text="hello"))
    assert result.status is TurnStatus.OK
    assert out["finals"] == []
    assert out["response"].text == "unknown: hello"


async def test_concurrent_turns_never_mix(harness_factory: Any) -> None:
    h = await harness_factory()
    feeds = {
        name: Feed(audio=tagged_audio(name), chunk_delay_s=delay)
        for name, delay in [("alice", 0.002), ("bob", 0.0), ("carol", 0.001)]
    }
    results = await asyncio.gather(*(h.turn(f) for f in feeds.values()))
    for name, (result, out) in zip(feeds, results, strict=True):
        assert result.status is TurnStatus.OK
        assert out["response"].text == f"{name}: heard {name}"


async def test_failed_speaker_fails_closed(harness_factory: Any) -> None:
    h = await harness_factory(lambda d: d["nodes"][2]["config"].update(mode="fail"))
    result, out = await h.turn(Feed(audio=tagged_audio("alice")))
    assert result.status is TurnStatus.DEGRADED
    assert result.nodes["spk"].status is NodeStatus.ERROR
    assert "model crashed" in (result.nodes["spk"].error or "")
    assert out["response"].text == "unknown: heard alice"


async def test_node_timeout_fails_closed(harness_factory: Any) -> None:
    h = await harness_factory(lambda d: d["nodes"][2]["config"].update(mode="hang"))
    result, out = await h.turn(Feed(audio=tagged_audio("alice")))
    assert result.nodes["spk"].status is NodeStatus.TIMEOUT
    assert result.status is TurnStatus.DEGRADED
    assert out["response"].text == "unknown: heard alice"


async def test_turn_deadline_cancels_everything(harness_factory: Any) -> None:
    h = await harness_factory(lambda d: d["nodes"][5]["config"].update(agent_id="hang"))
    result, out = await h.turn(Feed(text="hi"), deadline_s=0.2)
    assert result.status is TurnStatus.TIMEOUT
    assert result.nodes["agent"].status is NodeStatus.CANCELLED
    assert out == {}


async def test_wrong_payload_type_is_a_node_error(harness_factory: Any) -> None:
    h = await harness_factory(lambda d: d["nodes"][1]["config"].update(uri="bad-type"))
    result, out = await h.turn(Feed(audio=tagged_audio("alice")))
    assert result.nodes["stt"].status is NodeStatus.ERROR
    assert "expects text.utterance/1" in (result.nodes["stt"].error or "")
    assert out["response"] is None  # no utterance reached the agent


async def test_session_state_persists_across_turns(harness_factory: Any) -> None:
    h = await harness_factory()
    session_id = new_ulid()
    _, first = await h.turn(Feed(text="a"), session=new_session(0, session_id))
    _, second = await h.turn(Feed(text="b"), session=new_session(1, session_id))
    _, other = await h.turn(Feed(text="c"))
    assert first["response"].data == {"turns": 1}
    assert second["response"].data == {"turns": 2}
    assert other["response"].data == {"turns": 1}


def test_node_context_secrets() -> None:
    ctx = RuntimeNodeContext("llm", {}, {"key": "v", "other": "x"}, frozenset({"key"}))
    assert ctx.secret("key") == "v"
    with pytest.raises(KeyError):
        ctx.secret("other")
