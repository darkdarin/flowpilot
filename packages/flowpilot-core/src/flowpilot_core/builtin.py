"""Built-in generic nodes that real graphs need regardless of providers."""

from __future__ import annotations

from typing import ClassVar

from flowpilot_sdk import NodeContext, NodeSpec, PortSpec, TurnSession, node_translations

from .registry import NodeRegistry

UTTERANCE = "text.utterance/1"


class TextFirst:
    """Forward the first present utterance: ``primary`` (e.g. speech) else ``fallback`` (text)."""

    spec: ClassVar[NodeSpec] = NodeSpec(
        type="text.first",
        translations=node_translations(__package__, "text.first"),
        version="1.0.0",
        title="First utterance",
        category="logic",
        inputs={
            "primary": PortSpec(
                UTTERANCE,
                kind="value",
                required=False,
                description="Preferred source, usually the speech-to-text result.",
            ),
            "fallback": PortSpec(
                UTTERANCE,
                kind="value",
                required=False,
                description="Used when the primary input is absent, usually the typed text.",
            ),
        },
        outputs={
            "value": PortSpec(
                UTTERANCE, kind="value", description="The utterance that was present."
            )
        },
        description=(
            "Joins voice and text turns: one wire per input, the first value that is present wins."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        value = await turn.input("primary").value()
        if value is None:
            value = await turn.input("fallback").value()
        if value is not None:
            await turn.output("value").send(value)

    async def stop(self) -> None:
        return None


def register_builtin(registry: NodeRegistry) -> NodeRegistry:
    registry.register(TextFirst)
    return registry
