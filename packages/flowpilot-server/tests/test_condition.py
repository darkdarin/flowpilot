"""The Condition node: routes value to yes or no by the turn and by what comes on check."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import Deployment, ListTraceSink, RuntimeNodeContext, TurnStatus
from flowpilot_sdk import (
    Assurance,
    HostUnavailable,
    Node,
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
from flowpilot_server.condition import Condition
from flowpilot_server.manager import BUILTIN_GRANTS
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.quick import HaQuickCommands

PAVEL = Principal("u-pavel", frozenset(), Assurance.MEDIUM)


async def started(**config: Any) -> Condition:
    node = Condition()
    await node.start(RuntimeNodeContext("cond", config, {}, frozenset()))
    return node


def value(*items: Any, delay_s: float = 0.0) -> InputFeed:
    return InputFeed(items=list(items), meta={}, end="end", delay_s=delay_s)


async def route(node: Condition, inputs: dict[str, InputFeed], **kw: Any) -> tuple[str, Any]:
    run = await run_node_once(node, inputs, **kw)
    assert run.error is None, run.error
    taken = [name for name in ("yes", "no") if run.outputs[name].header is not None]
    assert len(taken) <= 1, taken  # never both
    branch = taken[0] if taken else "none"
    items = run.outputs[branch].items if taken else []
    return branch, items


async def test_without_conditions_value_goes_on_yes() -> None:
    branch, items = await route(await started(), {"value": value(Utterance("hi"))})
    assert branch == "yes" and items == [Utterance("hi")]


async def test_words_in_any_form_on_text() -> None:
    phrase = Utterance("Включи, пожалуйста, свет на кухне", "ru")
    feeds = {"value": value(phrase), "check": value(phrase)}
    assert (await route(await started(words=["включить"]), feeds))[0] == "yes"
    assert (await route(await started(words=["свет на кухня"]), feeds))[0] == "yes"  # a phrase
    assert (await route(await started(words=["выключить"]), feeds))[0] == "no"
    assert (await route(await started(pattern=r"кухн[яе]"), feeds))[0] == "yes"


async def test_a_text_stream_on_check_is_read_whole_and_value_is_kept_meanwhile() -> None:
    deltas = [TextDelta("Сейчас "), TextDelta("включу "), TextDelta("музыку")]
    node = await started(words=["музыка"])
    # value and check carry the same stream: the node keeps value while check is still coming
    branch, items = await route(
        node,
        {"value": value(*deltas), "check": value(*deltas, delay_s=0.02)},
        context={"language": "ru"},
    )
    assert branch == "yes" and items == deltas
    transcript = [Transcript("вклю", final=False), Transcript("включи свет", final=True)]
    branch, _ = await route(
        await started(words=["свет"]), {"value": value(*transcript), "check": value(*transcript)}
    )
    assert branch == "yes"


async def test_language_of_the_text_on_check() -> None:
    english = Utterance("What is your name", "en-US")
    feeds = {"value": value(english), "check": value(english)}
    assert (await route(await started(languages=["en"]), feeds))[0] == "yes"
    assert (await route(await started(languages=["ru", "uk"]), feeds))[0] == "no"
    # the text does not say: the turn's language
    unsaid = Utterance("Как тебя зовут")
    feeds = {"value": value(unsaid), "check": value(unsaid)}
    node = await started(languages=["ru"])
    assert (await route(node, feeds, context={"language": "ru-RU"}))[0] == "yes"
    assert (await route(node, feeds, context={"language": "en"}))[0] == "no"
    # an agent's whole answer
    answer = Response("My name is Jarvis", language="en")
    feeds = {"value": value(answer), "check": value(answer)}
    assert (await route(await started(languages=["en"]), feeds))[0] == "yes"


async def test_language_of_an_answer_stream_is_decided_as_it_opens() -> None:
    """The agent's stream says its language in its header: the branch is chosen at once, before
    the words, so text-to-speech after the condition starts as soon as the answer does."""
    deltas = [TextDelta("My name "), TextDelta("is Jarvis.")]
    english = {"language": "en"}
    node = await started(languages=["ru"])
    run = await run_node_once(
        node,
        {
            "value": InputFeed(items=deltas, meta=english, end="hang"),
            "check": InputFeed(items=deltas, meta=english, end="hang"),
        },
        context={"language": "ru"},
        cancel_after_s=0.3,
    )
    out = run.outputs["no"]
    assert out.header is not None and out.header.meta == english  # the language goes on
    assert out.items == deltas  # passed on while the answer is still coming
    assert run.outputs["yes"].header is None
    (decided,) = [e for e in run.trace.events if e.event == "logic.condition"]
    assert decided.attrs["why"] == "check ✗ (language en)"


async def test_language_of_a_stream_that_does_not_say_is_the_turns() -> None:
    deltas = [TextDelta("Привет")]
    node = await started(languages=["ru"])
    feeds = {"value": value(*deltas), "check": value(*deltas)}
    branch, _ = await route(node, feeds, context={"language": "ru"})
    assert branch == "yes"
    run = await run_node_once(node, feeds, context={"language": "ru"})
    (decided,) = [e for e in run.trace.events if e.event == "logic.condition"]
    assert decided.attrs["why"] == "check ✓ (language ru of the turn)"


async def test_data_came_or_not() -> None:
    node = await started(present="no")
    assert (await route(node, {"value": value(Utterance("x"))}))[0] == "yes"  # check absent
    feeds = {"value": value(Utterance("x")), "check": value(Utterance("done"))}
    assert (await route(node, feeds))[0] == "no"


async def test_who_speaks_on_check() -> None:
    node = await started(people=["u-pavel"])
    assert (await route(node, {"value": value(Utterance("x")), "check": value(PAVEL)}))[0] == "yes"
    unknown = {"value": value(Utterance("x")), "check": value(Principal.unknown())}
    assert (await route(node, unknown))[0] == "no"
    assert (await route(await started(known="unknown"), unknown))[0] == "yes"


async def test_who_speaks_can_be_routed_itself() -> None:
    node = await started(known="known")
    branch, items = await route(node, {"value": value(PAVEL), "check": value(PAVEL)})
    assert branch == "yes" and items == [PAVEL]


async def test_turn_kind_and_any_of() -> None:
    node = await started(turn="voice")
    assert (await route(node, {"value": value(Utterance("x"))}, context={"voice": True}))[
        0
    ] == "yes"
    assert (await route(node, {"value": value(Utterance("x"))}))[0] == "no"
    either = await started(turn="voice", words=["свет"], match="any")
    feeds = {"value": value(Utterance("x")), "check": value(Utterance("свет", "ru"))}
    assert (await route(either, feeds, context={"language": "ru"}))[0] == "yes"


async def test_time_spans_midnight_and_days() -> None:
    night = await started(time_from="22:00", time_to="07:00")
    assert night._time_fits(datetime(2026, 11, 9, 23, 30))
    assert night._time_fits(datetime(2026, 11, 9, 6, 59))
    assert not night._time_fits(datetime(2026, 11, 9, 7, 0))
    weekend = await started(days=["sat", "sun"])
    assert weekend._time_fits(datetime(2026, 11, 8, 12, 0))  # a Sunday
    assert not weekend._time_fits(datetime(2026, 11, 9, 12, 0))  # a Monday


class FakeWhere:
    def __init__(self, area: str | None) -> None:
        self.area = area

    def offers(self, op: str) -> bool:
        return True

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        if self.area is None:
            raise HostUnavailable(op)
        return {"area_id": self.area}

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


async def test_area_of_the_speakers_device() -> None:
    node = await started(areas=["kitchen"])
    feeds = {"value": value(Utterance("x"))}
    assert (await route(node, feeds, host=FakeWhere("kitchen")))[0] == "yes"
    assert (await route(node, feeds, host=FakeWhere("bedroom")))[0] == "no"
    assert (await route(node, feeds, host=FakeWhere(None)))[0] == "no"  # nobody can say where


async def test_nothing_on_value_goes_nowhere() -> None:
    assert (await route(await started(), {}))[0] == "none"


def test_a_graph_routes_who_speaks_through_a_condition() -> None:
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, Condition, HaQuickCommands):
        registry.register(cls)
    data = {
        "schema_version": 2,
        "id": "g",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "cond", "type": "logic.condition", "config": {"known": "known"}},
            {"id": "quick", "type": "ha.quick"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "in.principal", "to": "cond.value"},
            {"from": "in.principal", "to": "cond.check"},
            {"from": "cond.yes", "to": "quick.principal"},
            {"from": "in.utterance", "to": "quick.utterance"},
            {"from": "quick.response", "to": "out.response"},
        ],
    }
    vg = validate_graph(parse_graph(data), registry, grants=GrantPolicy(BUILTIN_GRANTS))
    assert vg.port_types[("out", "cond", "yes")] == "identity.principal/1"


async def test_conforms() -> None:
    sample = {"value": value(Utterance("x"))}
    assert_conforms(await check_node(Condition, config={}, sample=sample))


async def test_a_stream_is_passed_on_when_the_branch_is_decided_before_it_starts() -> None:
    """The bug of 0.1.28: decided at once (spoken or typed), the node waited for the stream's
    header in two places and lost every piece of an agent's answer that came later."""
    deltas = [TextDelta(f"кусок {i} ") for i in range(12)]
    node = await started(turn="voice")
    late = InputFeed(items=deltas, meta={}, end="end", delay_s=0.01, start_delay_s=0.05)
    branch, items = await route(node, {"value": late}, context={"voice": True})
    assert branch == "yes" and items == deltas


# --- in a real runtime: value and check fed by one output ----------------------------------------

ANSWER = [TextDelta(f"слово{i} ") for i in range(100)]  # more than a channel holds (32)


class Answer:
    """An agent's answer: many pieces, its language in the header."""

    spec = NodeSpec(
        type="test.answer",
        version="1.0.0",
        title="Answer",
        category="input",
        inputs={},
        outputs={"delta": PortSpec("text.delta/1", description="The answer.")},
        description="Test answer.",
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        out = turn.output("delta")
        await out.open({"language": "ru"})
        for delta in ANSWER:
            await out.send(delta)
        await out.end()


class Collect:
    spec = NodeSpec(
        type="test.collect",
        version="1.0.0",
        title="Collect",
        category="output",
        inputs={"text": PortSpec("text.delta/1", description="Text.")},
        outputs={},
        description="Test sink.",
    )

    def __init__(self) -> None:
        self.got: list[str] = []

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        self.got = [d.text async for d in turn.input("text")]


async def test_a_decision_by_the_header_does_not_hold_up_what_feeds_check() -> None:
    """The bug of 0.1.36: decided by the language in the header, the node never read check; fed
    by the same output as value, check filled up and stopped that output after 32 pieces, so the
    answer broke off mid-speech and the turn hung until its deadline."""
    registry = NodeRegistry()
    for cls in (Answer, Condition, Collect):
        registry.register(cast(type[Node], cls))
    data = {
        "schema_version": 2,
        "id": "g",
        "nodes": [
            {"id": "agent", "type": "test.answer"},
            {"id": "voice", "type": "logic.condition", "config": {}},
            {"id": "lang", "type": "logic.condition", "config": {"languages": ["ru"]}},
            {"id": "out", "type": "test.collect"},
        ],
        "edges": [
            {"from": "agent.delta", "to": "voice.value"},
            {"from": "voice.yes", "to": "lang.value"},
            {"from": "voice.yes", "to": "lang.check"},
            {"from": "lang.yes", "to": "out.text"},
        ],
    }
    vg = validate_graph(parse_graph(data), registry, grants=GrantPolicy(BUILTIN_GRANTS))
    deployment = Deployment(vg, registry, trace=ListTraceSink())
    await deployment.start()
    try:
        ctx = TurnContext(
            turn_id=new_ulid(),
            session_id="s",
            started_at=datetime.now(UTC),
            deadline_mono_ns=time.monotonic_ns() + int(3e9),
            source=SourceInfo(kind="test", adapter_id="test"),
            trace_id=new_ulid(),
            initial_principal=Principal.unknown(),
            language="ru",
        )
        session = SessionInfo(session_id="s", created_at=datetime.now(UTC), turn_index=0)
        result = await deployment.run_turn(ctx, session)
        assert result.status is TurnStatus.OK, result
        collected = cast(Collect, deployment.instance("out")).got
        assert collected == [d.text for d in ANSWER]
    finally:
        await deployment.stop()
