"""Spec-only fake nodes mirroring the example graph in Notion page 03."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from flowpilot_core import GrantPolicy, NodeRegistry
from flowpilot_sdk import (
    IDENTITY_PROVIDER_GRANT,
    Node,
    NodeContext,
    NodeSpec,
    PortSpec,
    TurnSession,
)

FIXTURES = Path(__file__).parent / "fixtures"


class SpecOnlyNode:
    """Base for fake nodes: validation only needs the spec."""

    spec: ClassVar[NodeSpec]

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        return None

    async def stop(self) -> None:
        return None


def make_node(spec: NodeSpec) -> type[Node]:
    return type(spec.type.replace(".", "_"), (SpecOnlyNode,), {"spec": spec})


AUDIO = "audio.pcm/1"
UTTERANCE = "text.utterance/1"


def example_specs() -> list[NodeSpec]:
    return [
        NodeSpec(
            type="ha.assist.source",
            version="1.0.0",
            title="Assist source",
            category="input",
            outputs={
                "audio": PortSpec(AUDIO),
                "utterance": PortSpec(UTTERANCE, kind="value"),
            },
        ),
        NodeSpec(
            type="wyoming.stt",
            version="1.2.0",
            title="Wyoming STT",
            category="speech",
            inputs={"audio": PortSpec(AUDIO)},
            outputs={
                "transcript": PortSpec("text.transcript/1"),
                "utterance": PortSpec(UTTERANCE, kind="value"),
            },
            config_schema={
                "type": "object",
                "properties": {"uri": {"type": "string"}, "language": {"type": "string"}},
                "required": ["uri"],
                "additionalProperties": False,
            },
        ),
        NodeSpec(
            type="speaker.onnx",
            version="1.0.0",
            title="Speaker ID",
            category="identity",
            inputs={"audio": PortSpec(AUDIO)},
            outputs={"speaker": PortSpec("identity.speaker/1", kind="value")},
            config_schema={
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["model"],
            },
        ),
        NodeSpec(
            type="identity.resolve",
            version="1.0.0",
            title="Identity resolver",
            category="identity",
            inputs={"speaker": PortSpec("identity.speaker/1", kind="value", required=False)},
            outputs={"principal": PortSpec("identity.principal/1", kind="value")},
            grants=frozenset({IDENTITY_PROVIDER_GRANT}),
            config_schema={
                "type": "object",
                "properties": {"timeout_ms": {"type": "integer", "minimum": 1}},
            },
        ),
        NodeSpec(
            type="text.first",
            version="1.0.0",
            title="First utterance",
            category="logic",
            inputs={
                "primary": PortSpec(UTTERANCE, kind="value", required=False),
                "fallback": PortSpec(UTTERANCE, kind="value", required=False),
            },
            outputs={"value": PortSpec(UTTERANCE, kind="value")},
        ),
        NodeSpec(
            type="ha.conversation",
            version="1.0.0",
            title="HA conversation",
            category="agent",
            inputs={
                "principal": PortSpec("identity.principal/1", kind="value"),
                "utterance": PortSpec(UTTERANCE, kind="value"),
            },
            outputs={"response": PortSpec("agent.response/1", kind="value")},
            config_schema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        ),
        NodeSpec(
            type="llm.cloud",
            version="1.0.0",
            title="Cloud LLM",
            category="agent",
            inputs={"utterance": PortSpec(UTTERANCE, kind="value")},
            outputs={"response": PortSpec("agent.response/1", kind="value")},
            config_schema={
                "type": "object",
                "properties": {"api_key": {"type": "secret"}, "model": {"type": "string"}},
                "required": ["api_key"],
            },
        ),
        NodeSpec(
            type="ha.assist.sink",
            version="1.0.0",
            title="Assist sink",
            category="output",
            inputs={
                "transcript": PortSpec("text.transcript/1", required=False),
                "response": PortSpec("agent.response/1", kind="value"),
                "speech": PortSpec(AUDIO, required=False),
            },
        ),
    ]


@pytest.fixture
def registry() -> NodeRegistry:
    reg = NodeRegistry()
    for spec in example_specs():
        reg.register(make_node(spec))
    return reg


@pytest.fixture
def grants() -> GrantPolicy:
    return GrantPolicy({"identity.resolve": frozenset({IDENTITY_PROVIDER_GRANT})})


@pytest.fixture
def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "family_assistant.json").read_text())
    return data
