"""Union port types and type variables (generic nodes) in graph validation."""

from __future__ import annotations

from typing import Any

import pytest

from flowpilot_core import GraphError, NodeRegistry, parse_graph, validate_graph
from flowpilot_sdk import ANY, TEXT, NodeSpec, PortSpec, union

from .conftest import make_node

DELTA = "text.delta/1"
UTTERANCE = "text.utterance/1"
AUDIO = "audio.pcm/1"


def spec(name: str, **kw: Any) -> NodeSpec:
    return NodeSpec(type=f"t.{name}", version="1.0.0", title=name, category="logic", **kw)


SPECS = [
    spec(
        "source",
        outputs={
            "utterance": PortSpec(UTTERANCE, kind="value"),
            "delta": PortSpec(DELTA),
            "audio": PortSpec(AUDIO),
        },
    ),
    # a generic node: both inputs and the output have one type, the one connected
    spec(
        "first",
        inputs={
            "a": PortSpec(TEXT, required=False, var="T"),
            "b": PortSpec(TEXT, required=False, var="T"),
        },
        outputs={"value": PortSpec(TEXT, var="T")},
    ),
    spec("text_sink", inputs={"text": PortSpec(TEXT, required=False)}),
    spec("utterance_sink", inputs={"utterance": PortSpec(UTTERANCE, kind="value", required=False)}),
    spec("delta_sink", inputs={"delta": PortSpec(DELTA, required=False)}),
    # gives one of two types without saying which: what it feeds must take both
    spec("either", outputs={"out": PortSpec(union(UTTERANCE, DELTA))}),
    spec("any_sink", inputs={"x": PortSpec(ANY, required=False)}),
]
SPECS[0] = NodeSpec(
    type="t.source", version="1.0.0", title="source", category="input", outputs=SPECS[0].outputs
)


@pytest.fixture
def registry() -> NodeRegistry:
    reg = NodeRegistry()
    for s in SPECS:
        reg.register(make_node(s))
    return reg


def graph(edges: list[tuple[str, str]], *nodes: tuple[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "g",
        "nodes": [{"id": "src", "type": "t.source"}]
        + [{"id": node_id, "type": f"t.{kind}"} for node_id, kind in nodes],
        "edges": [{"from": a, "to": b} for a, b in edges],
    }


def codes(data: dict[str, Any], registry: NodeRegistry) -> set[str]:
    try:
        validate_graph(parse_graph(data), registry)
    except GraphError as err:
        return err.codes
    return set()


def test_a_union_input_takes_any_of_its_types(registry: NodeRegistry) -> None:
    for port in ("utterance", "delta"):
        assert codes(graph([(f"src.{port}", "t.text")], ("t", "text_sink")), registry) == set()
    assert codes(graph([("src.audio", "t.text")], ("t", "text_sink")), registry) == {
        "type_mismatch"
    }


def test_a_generic_output_has_the_type_of_its_inputs(registry: NodeRegistry) -> None:
    data = graph(
        [("src.delta", "f.a"), ("f.value", "d.delta")], ("f", "first"), ("d", "delta_sink")
    )
    vg = validate_graph(parse_graph(data), registry)
    assert vg.port_types[("out", "f", "value")] == DELTA
    assert vg.port_types[("in", "f", "a")] == DELTA
    # the same output connected where an utterance is wanted does not fit
    wrong = graph(
        [("src.delta", "f.a"), ("f.value", "u.utterance")], ("f", "first"), ("u", "utterance_sink")
    )
    assert codes(wrong, registry) == {"type_mismatch"}


def test_generic_inputs_must_agree(registry: NodeRegistry) -> None:
    data = graph([("src.delta", "f.a"), ("src.utterance", "f.b")], ("f", "first"))
    assert codes(data, registry) == {"generic_mismatch"}


def test_generics_chain_and_need_an_input(registry: NodeRegistry) -> None:
    chained = graph(
        [("src.utterance", "f.a"), ("f.value", "g.b"), ("g.value", "u.utterance")],
        ("g", "first"),
        ("f", "first"),
        ("u", "utterance_sink"),
    )
    vg = validate_graph(parse_graph(chained), registry)
    assert vg.port_types[("out", "g", "value")] == UTTERANCE
    unresolved = graph([("f.value", "u.utterance")], ("f", "first"), ("u", "utterance_sink"))
    assert codes(unresolved, registry) == {"generic_unresolved"}


def test_an_unspecific_union_output_needs_an_input_taking_all(registry: NodeRegistry) -> None:
    assert (
        codes(graph([("e.out", "t.text")], ("e", "either"), ("t", "text_sink")), registry) == set()
    )
    narrow = graph([("e.out", "d.delta")], ("e", "either"), ("d", "delta_sink"))
    assert codes(narrow, registry) == {"type_mismatch"}


def test_port_types_are_checked() -> None:
    with pytest.raises(ValueError, match="repeats"):
        PortSpec(f"{DELTA}|{DELTA}")
    with pytest.raises(ValueError, match="invalid payload type id"):
        PortSpec("text.delta/1|nonsense")
    with pytest.raises(ValueError, match="no input does"):
        spec("bad", outputs={"x": PortSpec(TEXT, var="T")})


def test_an_any_input_takes_everything(registry: NodeRegistry) -> None:
    for port, type_id in (("utterance", UTTERANCE), ("delta", DELTA), ("audio", AUDIO)):
        data = graph([(f"src.{port}", "a.x")], ("a", "any_sink"))
        vg = validate_graph(parse_graph(data), registry)
        assert vg.port_types[("in", "a", "x")] == type_id
    # a union or a generic output fits too
    assert codes(graph([("e.out", "a.x")], ("e", "either"), ("a", "any_sink")), registry) == set()
    data = graph([("src.delta", "f.a"), ("f.value", "a.x")], ("f", "first"), ("a", "any_sink"))
    assert validate_graph(parse_graph(data), registry).port_types[("in", "a", "x")] == DELTA


def test_any_is_for_inputs_only() -> None:
    port = PortSpec(ANY)
    assert port.is_union and {DELTA, AUDIO} <= set(port.types)
    with pytest.raises(ValueError, match="cannot take any type"):
        spec("bad_any", outputs={"x": PortSpec(ANY)})
