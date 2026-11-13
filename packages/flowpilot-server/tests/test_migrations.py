"""Graphs saved by 0.1.2 load with today's debug nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flowpilot_core import GrantPolicy, NodeRegistry, parse_graph, validate_graph
from flowpilot_server.__main__ import build_registry
from flowpilot_server.manager import BUILTIN_GRANTS, GraphStore
from flowpilot_server.migrations import migrate_graph

OLD = {
    "schema_version": 1,
    "id": "old",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "stt", "type": "dev.fixed_stt", "config": {"text": "hi"}},
        {"id": "agent", "type": "dev.echo_agent", "config": {"prefix": "kids: "}},
        {
            "id": "say",
            "type": "debug.text",
            "version": "1.0.0",
            "config": {"text": "t", "when": "play"},
        },
        {"id": "rec", "type": "debug.record", "config": {"keep": 5}},
        {"id": "show", "type": "debug.text_tap"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.audio", "to": "stt.audio"},
        {"from": "in.audio", "to": "rec.audio"},
        {"from": "stt.utterance", "to": "agent.utterance"},
        {"from": "stt.utterance", "to": "say.after"},
        {"from": "say.utterance", "to": "show.utterance"},
        {"from": "say.delta", "to": "show.delta"},
        {"from": "agent.delta", "to": "out.delta"},
        {"from": "agent.response", "to": "out.response"},
    ],
}


def test_old_debug_nodes_are_replaced(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    store.save("old", OLD)
    data = store.load("old")
    assert data is not None
    types = {n["id"]: (n["type"], n.get("config")) for n in data["nodes"]}
    assert types["agent"] == ("debug.answer", {"text": "kids: {text}", "mode": "words"})
    assert types["say"] == ("debug.answer", {"text": "t"})
    assert "rec" not in types and "show" not in types  # probes, then removed with the journal
    edges = {(e["from"], e["to"]) for e in data["edges"]}
    assert not any(dst.startswith(("rec.", "show.")) for _, dst in edges)
    assert ("stt.utterance", "say.utterance") in edges
    assert not any(src == "say.utterance" for src, _ in edges)  # the output is gone
    registry: NodeRegistry = build_registry(dev=True)
    # raises if invalid
    validate_graph(parse_graph(data), registry, grants=GrantPolicy(BUILTIN_GRANTS))


def test_current_graphs_are_left_alone() -> None:
    data = {"schema_version": 2, "id": "g", "nodes": [{"id": "a", "type": "debug.answer"}]}
    assert migrate_graph(data) is data
    assert migrate_graph(migrate_graph(OLD)) == migrate_graph(OLD)


def test_voice_or_text_turn_becomes_a_condition_on_spoken_turns() -> None:
    """Old turn filters (one output, then two) end up as Conditions: spoken → yes, typed → no."""
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "voice", "type": "logic.turn_filter", "version": "1.0.0"},
            {"id": "chat", "type": "logic.turn_filter", "config": {"turns": "text"}},
            {"id": "new", "type": "logic.turn_filter"},
        ],
        "edges": [
            {"from": "a.delta", "to": "voice.text"},
            {"from": "voice.text", "to": "tts.text"},
            {"from": "chat.text", "to": "out.delta"},
            {"from": "new.typed", "to": "out.delta"},
        ],
    }
    migrated = migrate_graph(data)
    assert [(n["type"], n.get("config")) for n in migrated["nodes"]] == [
        ("logic.condition", {"turn": "voice"})
    ] * 3
    assert [(e["from"], e["to"]) for e in migrated["edges"]] == [
        ("a.delta", "voice.value"),
        ("voice.yes", "tts.text"),
        ("chat.no", "out.delta"),
        ("new.no", "out.delta"),
    ]
    assert migrate_graph(migrated) is migrated


def test_a_sound_signal_becomes_the_client_outputs_ack() -> None:
    """The owner's graph: Quick commands' ack played a Sound signal through First present into
    the speech; now ack goes to the client output, which confirms with the satellite's sound."""
    data = {
        "schema_version": 2,
        "id": "g",
        "nodes": [
            {"id": "quick", "type": "ha.quick"},
            {"id": "ding", "type": "audio.signal", "config": {"sound": "data:audio/wav;base64,AA"}},
            {"id": "sound", "type": "text.first"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "quick.ack", "to": "ding.trigger"},
            {"from": "ding.speech", "to": "sound.primary"},
            {"from": "tts.speech", "to": "sound.fallback"},
            {"from": "sound.value", "to": "out.speech"},
        ],
        "ui": {"nodes": {"ding": {"x": 1, "y": 2}, "quick": {"x": 3, "y": 4}}},
    }
    migrated = migrate_graph(data)
    assert [n["id"] for n in migrated["nodes"]] == ["quick", "sound", "out"]
    assert [(e["from"], e["to"]) for e in migrated["edges"]] == [
        ("tts.speech", "sound.fallback"),
        ("sound.value", "out.speech"),
        ("quick.ack", "out.ack"),
    ]
    assert "ding" not in migrated["ui"]["nodes"]
    assert migrate_graph(migrated) is migrated


def test_agent_remember_on_request_becomes_remembers_facts() -> None:
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "off", "type": "llm.agent", "config": {"model": "m", "tools": False}},
            {"id": "on", "type": "llm.agent", "config": {"model": "m", "tools": True}},
            {"id": "new", "type": "llm.agent", "config": {"model": "m", "remember": "auto"}},
        ],
        "edges": [],
    }
    migrated = migrate_graph(data)
    assert [n["config"] for n in migrated["nodes"]] == [
        {"model": "m", "remember": "off"},
        {"model": "m"},
        {"model": "m", "remember": "auto"},
    ]
    assert migrate_graph(migrated) is migrated


def test_converters_get_one_text_input_and_probes_go() -> None:
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "probe", "type": "debug.probe", "version": "1.0.0", "config": {"keep": 3}},
            {"id": "conv", "type": "text.convert"},
        ],
        "edges": [
            {"from": "a.response", "to": "probe.response"},
            {"from": "a.delta", "to": "probe.delta"},
            {"from": "in.audio", "to": "probe.audio"},
            {"from": "stt.transcript", "to": "conv.transcript"},
            {"from": "in.utterance", "to": "conv.utterance"},
        ],
        "ui": {"nodes": {"probe": {"x": 10, "y": 20}, "conv": {"x": 0, "y": 0}}},
    }
    migrated = migrate_graph(data)
    assert [n["id"] for n in migrated["nodes"]] == ["conv"]
    assert {(e["from"], e["to"]) for e in migrated["edges"]} == {
        ("stt.transcript", "conv.text"),  # the converter keeps its first input
    }
    assert migrated["ui"]["nodes"] == {"conv": {"x": 0, "y": 0}}
    assert migrate_graph(migrated) is migrated


def test_debug_probes_leave_the_graph_with_their_wires() -> None:
    """The journal of live debug shows what every node passed on: the probe is not needed."""
    data: dict[str, Any] = {
        "schema_version": 2,
        "id": "g",
        "nodes": [
            {"id": "in", "type": "client.source"},
            {"id": "probe", "type": "debug.probe"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [
            {"from": "in.audio", "to": "probe.value"},
            {"from": "in.utterance", "to": "out.response"},
        ],
    }
    migrated = migrate_graph(data)
    assert [n["id"] for n in migrated["nodes"]] == ["in", "out"]
    assert migrated["edges"] == [{"from": "in.utterance", "to": "out.response"}]
    assert data["nodes"][1]["type"] == "debug.probe"  # the saved graph is not changed in place
    assert migrate_graph(migrated) is migrated


def test_who_asks_is_wired_once_when_a_graph_of_schema_1_loads() -> None:
    data = {
        "schema_version": 1,
        "id": "g",
        "nodes": [
            {"id": "src", "type": "client.source"},
            {"id": "agent", "type": "llm.agent"},
            {"id": "ha", "type": "ha.conversation"},
            {"id": "quick", "type": "ha.quick"},
            {"id": "out", "type": "client.sink"},
        ],
        "edges": [{"from": "ident.principal", "to": "ha.principal"}],  # an identity node's: kept
    }
    migrated = migrate_graph(data)
    assert migrated["schema_version"] == 2
    assert {(e["from"], e["to"]) for e in migrated["edges"]} == {
        ("ident.principal", "ha.principal"),
        ("src.principal", "agent.principal"),
        ("src.principal", "quick.principal"),
    }
    # once: a wire removed later stays removed
    migrated["edges"] = [e for e in migrated["edges"] if e["to"] != "agent.principal"]
    assert migrate_graph(migrated) is migrated
