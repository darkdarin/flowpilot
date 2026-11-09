"""The Condition node: routes value to yes or no by the turn and by what comes on check."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import (
    Assurance,
    HostUnavailable,
    Principal,
    TextDelta,
    Transcript,
    Utterance,
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
