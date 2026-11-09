"""graph.json model and structural parsing (schema version 1).

Parsing checks only the shape of the document. Semantic checks that need node specs (types, ports,
config schemas, grants) live in :mod:`flowpilot_core.validate`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import GraphError, GraphIssue

# 2: who acts in a turn reaches nodes only by wire (the client input's ``principal`` output);
# runtimes migrate graphs of 1 by wiring it to every node that takes it.
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
GRAPH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DEFAULT_TURN_TIMEOUT_MS = 30_000


@dataclass(frozen=True, slots=True)
class PortRef:
    node: str
    port: str

    def __str__(self) -> str:
        return f"{self.node}.{self.port}"


@dataclass(frozen=True, slots=True)
class NodeDecl:
    id: str
    type: str
    version: str
    config: Mapping[str, Any] = field(default_factory=dict)
    # settings made inputs of the node (``DYNAMIC_KEY``): the node gets an input of each name
    setting_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EdgeDecl:
    src: PortRef
    dst: PortRef
    buffer: int | None = None


@dataclass(frozen=True, slots=True)
class GraphSettings:
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class Graph:
    id: str
    name: str
    nodes: tuple[NodeDecl, ...]
    edges: tuple[EdgeDecl, ...]
    settings: GraphSettings = GraphSettings()
    schema_version: int = SCHEMA_VERSION
    ui: Mapping[str, Any] = field(default_factory=dict)

    def node(self, node_id: str) -> NodeDecl:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to graph.json form."""
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "settings": {"turn_timeout_ms": self.settings.turn_timeout_ms},
            "nodes": [
                {"id": n.id, "type": n.type, "version": n.version, "config": dict(n.config)}
                | ({"setting_inputs": list(n.setting_inputs)} if n.setting_inputs else {})
                for n in self.nodes
            ],
            "edges": [
                {"from": str(e.src), "to": str(e.dst)} | ({"buffer": e.buffer} if e.buffer else {})
                for e in self.edges
            ],
        }
        if self.ui:
            data["ui"] = dict(self.ui)
        return data


def parse_port_ref(value: Any) -> PortRef | None:
    """Parse ``"node.port"``; ``None`` if malformed. Node ids have no dots, ports may not either."""
    if not isinstance(value, str) or value.count(".") != 1:
        return None
    node, port = value.split(".")
    if not NODE_ID_RE.match(node) or not NODE_ID_RE.match(port):
        return None
    return PortRef(node, port)


def parse_graph(data: Any) -> Graph:
    """Parse a graph.json document; raise :class:`GraphError` listing every structural issue."""
    issues: list[GraphIssue] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append(GraphIssue(code, path, message))

    if not isinstance(data, Mapping):
        raise GraphError([GraphIssue("not_an_object", "$", "graph must be a JSON object")])

    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise GraphError(
            [
                GraphIssue(
                    "unsupported_schema_version",
                    "$.schema_version",
                    f"unsupported schema_version {version!r}, "
                    f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
                )
            ]
        )

    graph_id = data.get("id")
    if not isinstance(graph_id, str) or not GRAPH_ID_RE.match(graph_id):
        issue("invalid_graph_id", "$.id", "id must match [a-z0-9][a-z0-9_-]*")
        graph_id = ""
    name = data.get("name", graph_id)
    if not isinstance(name, str):
        issue("invalid_name", "$.name", "name must be a string")
        name = ""

    settings = GraphSettings()
    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, Mapping):
        issue("invalid_settings", "$.settings", "settings must be an object")
    else:
        timeout = raw_settings.get("turn_timeout_ms", DEFAULT_TURN_TIMEOUT_MS)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            issue(
                "invalid_turn_timeout",
                "$.settings.turn_timeout_ms",
                "turn_timeout_ms must be a positive integer",
            )
        else:
            settings = GraphSettings(turn_timeout_ms=timeout)

    nodes: list[NodeDecl] = []
    seen_ids: set[str] = set()
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        issue("invalid_nodes", "$.nodes", "nodes must be a list")
        raw_nodes = []
    for i, raw in enumerate(raw_nodes):
        path = f"$.nodes[{i}]"
        if not isinstance(raw, Mapping):
            issue("invalid_node", path, "node must be an object")
            continue
        node_id, node_type, version = raw.get("id"), raw.get("type"), raw.get("version", "*")
        config = raw.get("config", {})
        setting_inputs = raw.get("setting_inputs", [])
        ok = True
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            issue("invalid_node_id", f"{path}.id", "node id must match [a-z][a-z0-9_]*")
            ok = False
        elif node_id in seen_ids:
            issue("duplicate_node_id", f"{path}.id", f"duplicate node id {node_id!r}")
            ok = False
        if not isinstance(node_type, str) or not node_type:
            issue("invalid_node_type", f"{path}.type", "type must be a non-empty string")
            ok = False
        if not isinstance(version, str) or not version:
            issue("invalid_version_range", f"{path}.version", "version must be a string")
            ok = False
        if not isinstance(config, Mapping):
            issue("invalid_config", f"{path}.config", "config must be an object")
            ok = False
        if not isinstance(setting_inputs, list) or not all(
            isinstance(k, str) and k for k in setting_inputs
        ):
            issue(
                "invalid_setting_inputs",
                f"{path}.setting_inputs",
                "setting_inputs must be a list of setting names",
            )
            ok = False
        if ok:
            assert isinstance(node_id, str) and isinstance(node_type, str)
            assert isinstance(version, str) and isinstance(config, Mapping)
            seen_ids.add(node_id)
            nodes.append(
                NodeDecl(
                    node_id, node_type, version, dict(config), tuple(dict.fromkeys(setting_inputs))
                )
            )

    edges: list[EdgeDecl] = []
    raw_edges = data.get("edges", [])
    if not isinstance(raw_edges, list):
        issue("invalid_edges", "$.edges", "edges must be a list")
        raw_edges = []
    for i, raw in enumerate(raw_edges):
        path = f"$.edges[{i}]"
        if not isinstance(raw, Mapping):
            issue("invalid_edge", path, "edge must be an object")
            continue
        src, dst = parse_port_ref(raw.get("from")), parse_port_ref(raw.get("to"))
        buffer = raw.get("buffer")
        ok = True
        if src is None:
            issue("invalid_port_ref", f"{path}.from", "expected 'node.port'")
            ok = False
        if dst is None:
            issue("invalid_port_ref", f"{path}.to", "expected 'node.port'")
            ok = False
        if buffer is not None and (
            not isinstance(buffer, int) or isinstance(buffer, bool) or buffer < 1
        ):
            issue("invalid_buffer", f"{path}.buffer", "buffer must be a positive integer")
            ok = False
        if ok:
            assert src is not None and dst is not None
            edges.append(EdgeDecl(src, dst, buffer))

    ui = data.get("ui", {})
    if not isinstance(ui, Mapping):
        issue("invalid_ui", "$.ui", "ui must be an object")
        ui = {}

    if issues:
        raise GraphError(issues)
    return Graph(
        id=graph_id,
        name=name,
        nodes=tuple(nodes),
        edges=tuple(edges),
        settings=settings,
        schema_version=SCHEMA_VERSION,
        ui=dict(ui),
    )
