"""graph.json parsing (structure only)."""

from __future__ import annotations

from typing import Any

import pytest

from flowpilot_core import GraphError, PortRef, parse_graph


def test_example_parses(example: dict[str, Any]) -> None:
    g = parse_graph(example)
    assert g.id == "family-assistant"
    assert len(g.nodes) == 8 and len(g.edges) == 11
    assert g.edges[0].src == PortRef("in", "audio")
    assert g.settings.turn_timeout_ms == 30000
    assert g.ui["nodes"]["in"]["x"] == 40


def test_roundtrip(example: dict[str, Any]) -> None:
    g = parse_graph(example)
    assert parse_graph(g.to_dict()) == g


def test_defaults() -> None:
    g = parse_graph(
        {"schema_version": 1, "id": "g", "nodes": [{"id": "a", "type": "x.y"}], "edges": []}
    )
    assert g.name == "g"
    assert g.nodes[0].version == "*"
    assert g.settings.turn_timeout_ms == 30000


@pytest.mark.parametrize("version", [None, 0, 3, "1"])
def test_unsupported_schema_version(version: Any) -> None:
    with pytest.raises(GraphError) as exc:
        parse_graph({"schema_version": version, "id": "g", "nodes": []})
    assert exc.value.codes == {"unsupported_schema_version"}


def test_not_an_object() -> None:
    with pytest.raises(GraphError) as exc:
        parse_graph([])
    assert exc.value.codes == {"not_an_object"}


def test_all_structural_issues_reported_at_once() -> None:
    data = {
        "schema_version": 1,
        "id": "Bad Id",
        "settings": {"turn_timeout_ms": -1},
        "nodes": [
            {"id": "a", "type": "x.y"},
            {"id": "a", "type": "x.y"},
            {"id": "B", "type": "x.y"},
            {"id": "c", "type": "", "config": []},
        ],
        "edges": [
            {"from": "a", "to": "a.in"},
            {"from": "a.out", "to": "a.in.extra"},
            {"from": "a.out", "to": "a.in", "buffer": 0},
        ],
    }
    with pytest.raises(GraphError) as exc:
        parse_graph(data)
    assert exc.value.codes == {
        "invalid_graph_id",
        "invalid_turn_timeout",
        "duplicate_node_id",
        "invalid_node_id",
        "invalid_node_type",
        "invalid_config",
        "invalid_port_ref",
        "invalid_buffer",
    }
    assert len([i for i in exc.value.issues if i.code == "invalid_port_ref"]) == 2
