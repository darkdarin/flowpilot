"""Settings made inputs of a node: what comes on them overrides the setting for the turn."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from flowpilot_core import GrantPolicy, GraphError, NodeRegistry, parse_graph, validate_graph
from flowpilot_core.builtin import register_builtin
from flowpilot_core.runtime import Deployment, ListTraceSink
from flowpilot_core.runtime.settings import coerce
from flowpilot_sdk import (
    DYNAMIC_KEY,
    NodeContext,
    NodeSpec,
    Principal,
    SessionInfo,
    SourceInfo,
    TurnContext,
    TurnSession,
    new_ulid,
)

SEEN: list[dict[str, Any]] = []


class Start:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="t.start", version="1.0.0", title="Start", category="input", description="Start."
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        return None


class Speaker:
    """Records the settings it gets in each turn."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="t.speaker",
        version="1.0.0",
        title="Speaker",
        category="speech",
        description="Speaks.",
        config_schema={
            "type": "object",
            "properties": {
                "speed": {
                    "title": "Speed",
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 2,
                    DYNAMIC_KEY: True,
                },
                "whisper": {"title": "Whisper", "type": "boolean", DYNAMIC_KEY: True},
                "model": {"title": "Model", "type": "string"},  # not for a turn
            },
        },
    )

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}

    async def start(self, ctx: NodeContext) -> None:
        self.config = dict(ctx.config)

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        SEEN.append(await turn.settings(self.config))


def registry() -> NodeRegistry:
    reg = register_builtin(NodeRegistry())
    reg.register(Start)
    reg.register(Speaker)
    return reg


def graph(value: str | None, *, inputs: list[str] | None = None) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {"id": "start", "type": "t.start"},
        {
            "id": "tts",
            "type": "t.speaker",
            "config": {"speed": 1.0, "model": "m1"},
            "setting_inputs": inputs if inputs is not None else ["speed"],
        },
    ]
    edges = []
    if value is not None:
        nodes.append({"id": "night", "type": "logic.value", "config": {"value": value}})
        edges.append({"from": "night.value", "to": "tts.speed"})
    return {"schema_version": 2, "id": "g", "nodes": nodes, "edges": edges}


async def one_turn(data: dict[str, Any]) -> tuple[dict[str, Any], ListTraceSink]:
    SEEN.clear()
    trace = ListTraceSink()
    deployment = Deployment(validate_graph(parse_graph(data), registry()), registry(), trace=trace)
    await deployment.start()
    try:
        session = SessionInfo(new_ulid(), datetime.now(UTC), 0)
        ctx = TurnContext(
            turn_id=new_ulid(),
            session_id=session.session_id,
            started_at=datetime.now(UTC),
            deadline_mono_ns=time.monotonic_ns() + 5_000_000_000,
            source=SourceInfo(kind="test", adapter_id="test"),
            trace_id=new_ulid(),
            initial_principal=Principal.unknown(),
        )
        await deployment.run_turn(ctx, session)
    finally:
        await deployment.stop()
    return SEEN[-1], trace


async def test_a_value_on_a_setting_input_overrides_the_setting_for_the_turn() -> None:
    settings, _ = await one_turn(graph("0,8"))  # text, with a comma: read as the number
    assert settings == {"speed": 0.8, "model": "m1"}


async def test_nothing_on_the_input_keeps_the_setting() -> None:
    settings, _ = await one_turn(graph(None))
    assert settings == {"speed": 1.0, "model": "m1"}


async def test_a_value_that_does_not_fit_is_left_out() -> None:
    settings, trace = await one_turn(graph("5"))  # above the maximum
    assert settings["speed"] == 1.0
    (event,) = [e for e in trace.events if e.event == "settings.invalid"]
    assert event.attrs["setting"] == "speed"


def test_only_settings_marked_dynamic_become_inputs() -> None:
    vg = validate_graph(parse_graph(graph("1")), registry(), grants=GrantPolicy())
    port = vg.specs["tts"].inputs["speed"]
    assert port.type == "config.value/1" and port.kind == "value" and not port.required
    with pytest.raises(GraphError) as err:
        validate_graph(parse_graph(graph(None, inputs=["model"])), registry())
    assert "setting_not_dynamic" in err.value.codes


def test_setting_inputs_are_saved_with_the_graph() -> None:
    parsed = parse_graph(graph(None, inputs=["speed", "whisper"]))
    node = parsed.to_dict()["nodes"][1]
    assert node["setting_inputs"] == ["speed", "whisper"]
    assert "setting_inputs" not in parsed.to_dict()["nodes"][0]


def test_values_take_the_type_of_the_setting() -> None:
    assert coerce("да", {"type": "boolean"}) is True
    assert coerce("off", {"type": "boolean"}) is False
    assert coerce("3", {"type": "integer"}) == 3
    assert coerce("flux-olga-ru", {"type": "string"}) == "flux-olga-ru"
    assert coerce(2, {"type": "string"}) == "2"
    assert coerce("2", {"enum": [1, 2, 3]}) == 2
    assert coerce("fast", {"type": "number"}) == "fast"  # does not read as one: left for the check
