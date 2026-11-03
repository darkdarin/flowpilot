"""Semantic graph validation against node specs."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from flowpilot_core import (
    GrantPolicy,
    GraphError,
    NodeRegistry,
    parse_graph,
    validate_graph,
)
from flowpilot_sdk import NodeSpec, PortSpec

from .conftest import make_node


def check(
    data: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy | None = None
) -> set[str]:
    """Validate and return the set of issue codes (empty if valid)."""
    try:
        validate_graph(parse_graph(data), registry, grants=grants)
    except GraphError as err:
        return err.codes
    return set()


def edges_without(data: dict[str, Any], dst: str) -> dict[str, Any]:
    out = copy.deepcopy(data)
    out["edges"] = [e for e in out["edges"] if e["to"] != dst]
    return out


def test_example_is_valid(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    vg = validate_graph(parse_graph(example), registry, grants=grants)
    assert vg.source == "in"
    assert vg.order[0] == "in" and vg.order[-1] == "out"
    assert vg.order.index("utt") < vg.order.index("agent")
    assert {e.dst.node for e in vg.outgoing["in"]["audio"]} == {"stt", "spk"}
    assert vg.incoming["agent"]["utterance"].src.node == "utt"


def test_identity_provider_needs_deployment_grant(
    example: dict[str, Any], registry: NodeRegistry
) -> None:
    assert check(example, registry) == {"grant_not_permitted"}


def test_unknown_type_and_versions(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    data = copy.deepcopy(example)
    data["nodes"][1]["type"] = "whisper.stt"
    data["nodes"][2]["version"] = "^2"
    data["nodes"][4]["version"] = "latest"
    codes = check(data, registry, grants)
    assert {"unknown_node_type", "version_mismatch", "invalid_version_range"} <= codes


def test_port_errors(example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy) -> None:
    data = edges_without(example, "agent.utterance")
    data["edges"] += [
        {"from": "spk.speaker", "to": "agent.utterance"},  # type mismatch
        {"from": "stt.words", "to": "out.transcript"},  # unknown output port
        {"from": "ghost.x", "to": "out.speech"},  # unknown node
    ]
    codes = check(data, registry, grants)
    assert {"type_mismatch", "unknown_port", "unknown_node"} <= codes


def test_input_connected_twice(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    data = copy.deepcopy(example)
    data["edges"].append({"from": "stt.utterance", "to": "agent.utterance"})
    assert check(data, registry, grants) == {"input_already_connected"}


def test_required_input_unconnected(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    data = edges_without(example, "agent.utterance")
    assert check(data, registry, grants) == {"required_input_unconnected"}


def test_optional_inputs_may_stay_unconnected(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    data = edges_without(edges_without(example, "out.transcript"), "utt.fallback")
    assert check(data, registry, grants) == set()


def test_kind_mismatch(registry: NodeRegistry) -> None:
    registry.register(
        make_node(
            NodeSpec(
                type="test.stream_sink",
                version="1.0.0",
                title="T",
                category="output",
                inputs={"utterance": PortSpec("text.utterance/1", kind="stream")},
            )
        )
    )
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "in", "type": "ha.assist.source"},
            {"id": "t", "type": "test.stream_sink"},
        ],
        "edges": [{"from": "in.utterance", "to": "t.utterance"}],
    }
    assert check(data, registry) == {"kind_mismatch", "required_input_unconnected"}


def test_cycle(registry: NodeRegistry) -> None:
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "in", "type": "ha.assist.source"},
            {"id": "a", "type": "text.first"},
            {"id": "b", "type": "text.first"},
        ],
        "edges": [
            {"from": "in.utterance", "to": "a.primary"},
            {"from": "a.value", "to": "b.primary"},
            {"from": "b.value", "to": "a.fallback"},
        ],
    }
    assert check(data, registry) == {"cycle"}


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_source(count: int, registry: NodeRegistry) -> None:
    nodes = [{"id": f"in{i}", "type": "ha.assist.source"} for i in range(count)]
    data = {"schema_version": 1, "id": "g", "nodes": nodes, "edges": []}
    assert check(data, registry) == {"source_count"}


def test_config_validation(
    example: dict[str, Any], registry: NodeRegistry, grants: GrantPolicy
) -> None:
    data = copy.deepcopy(example)
    del data["nodes"][1]["config"]["uri"]
    data["nodes"][2]["config"]["threshold"] = 1.5
    with pytest.raises(GraphError) as exc:
        validate_graph(parse_graph(data), registry, grants=grants)
    paths = {i.path for i in exc.value.issues if i.code == "invalid_config"}
    assert paths == {"$.nodes[1].config", "$.nodes[2].config.threshold"}


def _llm_graph(api_key: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "in", "type": "ha.assist.source"},
            {"id": "llm", "type": "llm.cloud", "config": {"api_key": api_key}},
            {"id": "out", "type": "ha.assist.sink"},
        ],
        "edges": [
            {"from": "in.utterance", "to": "llm.utterance"},
            {"from": "llm.response", "to": "out.response"},
        ],
    }


def test_secret_reference_accepted_and_collected(registry: NodeRegistry) -> None:
    vg = validate_graph(parse_graph(_llm_graph({"secret": "openai_key"})), registry)
    assert vg.secret_refs["llm"] == frozenset({"openai_key"})
    assert vg.secret_refs["in"] == frozenset()


@pytest.mark.parametrize("value", ["sk-raw-key-in-graph", {"secret": ""}, {"secret": "a", "x": 1}])
def test_raw_or_malformed_secret_rejected(value: Any, registry: NodeRegistry) -> None:
    assert check(_llm_graph(value), registry) == {"invalid_config"}
