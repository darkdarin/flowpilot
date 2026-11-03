"""Registry of installed node types."""

from __future__ import annotations

from flowpilot_sdk import Node, NodeSpec


class NodeRegistry:
    """Installed node implementations, one version per node type."""

    def __init__(self) -> None:
        self._nodes: dict[str, type[Node]] = {}

    def register(self, node_cls: type[Node]) -> type[Node]:
        spec = node_cls.spec
        existing = self._nodes.get(spec.type)
        if existing is not None and existing is not node_cls:
            raise ValueError(f"node type {spec.type!r} is already registered by {existing!r}")
        self._nodes[spec.type] = node_cls
        return node_cls

    def get(self, node_type: str) -> type[Node] | None:
        return self._nodes.get(node_type)

    def spec(self, node_type: str) -> NodeSpec | None:
        node_cls = self._nodes.get(node_type)
        return None if node_cls is None else node_cls.spec

    def types(self) -> list[str]:
        return sorted(self._nodes)
