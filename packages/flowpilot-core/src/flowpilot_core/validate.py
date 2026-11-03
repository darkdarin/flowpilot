"""Semantic graph validation against installed node specs (contracts v1, Notion page 03 §7)."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from flowpilot_sdk import NodeSpec, PortSpec

from .errors import GraphError, GraphIssue
from .graph import EdgeDecl, Graph
from .registry import NodeRegistry
from .versions import satisfies, validate_range

SECRET_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"secret": {"type": "string", "minLength": 1}},
    "required": ["secret"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class GrantPolicy:
    """Which grants each node type is trusted with in this deployment."""

    allowed: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def permits(self, node_type: str, grant: str) -> bool:
        return grant in self.allowed.get(node_type, frozenset())


@dataclass(frozen=True, slots=True)
class ValidatedGraph:
    """A graph that passed validation, with everything the runtime needs precomputed."""

    graph: Graph
    specs: Mapping[str, NodeSpec]  # node id -> spec
    order: tuple[str, ...]  # topological order of node ids
    incoming: Mapping[str, Mapping[str, EdgeDecl]]  # node id -> input port -> edge
    outgoing: Mapping[str, Mapping[str, tuple[EdgeDecl, ...]]]  # node id -> output -> edges
    secret_refs: Mapping[str, frozenset[str]]  # node id -> referenced secret names
    source: str  # node id of the single input adapter


def secret_aware_schema(schema: Any) -> Any:
    """Replace ``{"type": "secret"}`` markers with the secret reference object schema."""
    if isinstance(schema, Mapping):
        if schema.get("type") == "secret":
            return copy.deepcopy(SECRET_REF_SCHEMA)
        return {k: secret_aware_schema(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [secret_aware_schema(v) for v in schema]
    return schema


def collect_secret_refs(value: Any, out: set[str]) -> None:
    if isinstance(value, Mapping):
        if set(value) == {"secret"} and isinstance(value["secret"], str):
            out.add(value["secret"])
            return
        for v in value.values():
            collect_secret_refs(v, out)
    elif isinstance(value, list):
        for v in value:
            collect_secret_refs(v, out)


def validate_graph(
    graph: Graph, registry: NodeRegistry, *, grants: GrantPolicy | None = None
) -> ValidatedGraph:
    """Validate ``graph``; raise :class:`GraphError` listing every issue found."""
    grants = grants or GrantPolicy()
    issues: list[GraphIssue] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append(GraphIssue(code, path, message))

    specs: dict[str, NodeSpec] = {}
    secret_refs: dict[str, frozenset[str]] = {}
    sources: list[str] = []

    for i, node in enumerate(graph.nodes):
        path = f"$.nodes[{i}]"
        spec = registry.spec(node.type)
        if spec is None:
            issue("unknown_node_type", f"{path}.type", f"node type {node.type!r} is not installed")
            continue
        try:
            validate_range(node.version)
        except ValueError as err:
            issue("invalid_version_range", f"{path}.version", str(err))
            continue
        if not satisfies(spec.version, node.version):
            issue(
                "version_mismatch",
                f"{path}.version",
                f"installed {node.type} {spec.version} does not satisfy {node.version!r}",
            )
            continue
        specs[node.id] = spec

        try:
            validator = Draft202012Validator(secret_aware_schema(spec.config_schema))
            errors = sorted(validator.iter_errors(dict(node.config)), key=lambda e: list(e.path))
        except SchemaError as schema_err:
            issue(
                "invalid_config_schema",
                f"{path}.type",
                f"{node.type}: bad config schema: {schema_err.message}",
            )
            errors = []
        for config_err in errors:
            where = "".join(f"[{p!r}]" if isinstance(p, int) else f".{p}" for p in config_err.path)
            issue("invalid_config", f"{path}.config{where}", config_err.message)
        refs: set[str] = set()
        collect_secret_refs(node.config, refs)
        secret_refs[node.id] = frozenset(refs)

        for grant in sorted(spec.grants):
            if not grants.permits(node.type, grant):
                issue(
                    "grant_not_permitted",
                    f"{path}.type",
                    f"{node.type} requires grant {grant!r}, not permitted in this deployment",
                )
        if spec.category == "input":
            sources.append(node.id)

    if len(sources) != 1:
        issue(
            "source_count",
            "$.nodes",
            f"graph needs exactly one input adapter node, found {len(sources)}",
        )

    incoming: dict[str, dict[str, EdgeDecl]] = defaultdict(dict)
    outgoing: dict[str, dict[str, list[EdgeDecl]]] = defaultdict(lambda: defaultdict(list))
    adjacency: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    node_ids = set(adjacency)

    for i, edge in enumerate(graph.edges):
        path = f"$.edges[{i}]"
        src_spec, dst_spec = specs.get(edge.src.node), specs.get(edge.dst.node)
        if edge.src.node not in node_ids:
            issue("unknown_node", f"{path}.from", f"no node {edge.src.node!r}")
        if edge.dst.node not in node_ids:
            issue("unknown_node", f"{path}.to", f"no node {edge.dst.node!r}")
        if src_spec is None or dst_spec is None:
            continue  # node missing or already reported
        src_port: PortSpec | None = src_spec.outputs.get(edge.src.port)
        dst_port: PortSpec | None = dst_spec.inputs.get(edge.dst.port)
        if src_port is None:
            issue(
                "unknown_port", f"{path}.from", f"{src_spec.type} has no output {edge.src.port!r}"
            )
        if dst_port is None:
            issue("unknown_port", f"{path}.to", f"{dst_spec.type} has no input {edge.dst.port!r}")
        if src_port is None or dst_port is None:
            continue
        if src_port.type != dst_port.type:
            issue(
                "type_mismatch",
                path,
                f"{edge.src} ({src_port.type}) cannot connect to {edge.dst} ({dst_port.type})",
            )
            continue
        if src_port.kind != dst_port.kind:
            issue(
                "kind_mismatch",
                path,
                f"{edge.src} is a {src_port.kind} port, {edge.dst} is a {dst_port.kind} port",
            )
            continue
        if edge.dst.port in incoming[edge.dst.node]:
            issue("input_already_connected", f"{path}.to", f"{edge.dst} already has an edge")
            continue
        incoming[edge.dst.node][edge.dst.port] = edge
        outgoing[edge.src.node][edge.src.port].append(edge)
        adjacency[edge.src.node].add(edge.dst.node)

    for i, node in enumerate(graph.nodes):
        spec = specs.get(node.id)
        if spec is None:
            continue
        for port_name, port in spec.inputs.items():
            if port.required and port_name not in incoming[node.id]:
                issue(
                    "required_input_unconnected",
                    f"$.nodes[{i}]",
                    f"required input {node.id}.{port_name} is not connected",
                )

    order = _topological_order(graph, adjacency)
    if order is None:
        issue("cycle", "$.edges", "graph contains a cycle")

    if issues:
        raise GraphError(issues)
    assert order is not None
    return ValidatedGraph(
        graph=graph,
        specs=specs,
        order=tuple(order),
        incoming={k: dict(v) for k, v in incoming.items()},
        outgoing={k: {p: tuple(es) for p, es in v.items()} for k, v in outgoing.items()},
        secret_refs=secret_refs,
        source=sources[0],
    )


def _topological_order(graph: Graph, adjacency: Mapping[str, set[str]]) -> list[str] | None:
    """Kahn's algorithm in declaration order; ``None`` if there is a cycle."""
    indegree = {n.id: 0 for n in graph.nodes}
    for targets in adjacency.values():
        for t in targets:
            indegree[t] += 1
    ready = [n.id for n in graph.nodes if indegree[n.id] == 0]
    order: list[str] = []
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for t in sorted(adjacency[node_id], key=[n.id for n in graph.nodes].index):
            indegree[t] -= 1
            if indegree[t] == 0:
                ready.append(t)
    return order if len(order) == len(graph.nodes) else None
