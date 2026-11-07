"""Semantic graph validation against installed node specs (contracts v1, Notion page 03 §7)."""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from flowpilot_sdk import NodeSpec, PortSpec, type_kind

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
    # The one payload type a connected port carries in this graph, where it is known: ports of
    # one type, and union ports resolved by a type variable or by what is connected. Keys are
    # ("in" | "out", node id, port).
    port_types: Mapping[tuple[str, str, str], str] = field(default_factory=dict)


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

    wired: list[tuple[str, EdgeDecl, PortSpec, PortSpec]] = []
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
        wired.append((path, edge, src_port, dst_port))

    # Type variables: a generic node's inputs with the same variable take the type connected to
    # the first of them, and its outputs with it give that type. Bindings flow along the edges.
    bindings: dict[tuple[str, str], str] = {}

    def carried(node: str, port: PortSpec) -> str | None:
        """The one type an output gives in this graph, if known."""
        if port.var:
            return bindings.get((node, port.var))
        return None if port.is_union else port.type

    changed = True
    while changed:
        changed = False
        for _, edge, src_port, dst_port in wired:
            given = carried(edge.src.node, src_port)
            key = (edge.dst.node, dst_port.var or "")
            if given and dst_port.var and key not in bindings and given in dst_port.types:
                bindings[key] = given
                changed = True

    def kind_of(port: PortSpec, given: str | None) -> str | None:
        if not port.is_union:
            return port.kind
        try:
            return type_kind(given) if given else None
        except LookupError:
            return None

    port_types: dict[tuple[str, str, str], str] = {}
    for path, edge, src_port, dst_port in wired:
        given = carried(edge.src.node, src_port)
        if src_port.var and given is None:
            issue(
                "generic_unresolved",
                path,
                f"{edge.src} gives the type connected to the inputs of {edge.src.node}: "
                "connect one of them",
            )
            continue
        types = (given,) if given else src_port.types
        if not set(types) <= set(dst_port.types):
            issue(
                "type_mismatch",
                path,
                f"{edge.src} ({given or src_port.type}) cannot connect to {edge.dst} "
                f"({dst_port.type})",
            )
            continue
        bound = bindings.get((edge.dst.node, dst_port.var)) if dst_port.var else None
        if bound and given and given != bound:
            issue(
                "generic_mismatch",
                path,
                f"{edge.dst} must take the same type as the other inputs of {edge.dst.node} "
                f"({bound}), not {given}",
            )
            continue
        src_kind, dst_kind = kind_of(src_port, given), kind_of(dst_port, given)
        if src_kind and dst_kind and src_kind != dst_kind:
            issue(
                "kind_mismatch",
                path,
                f"{edge.src} is a {src_kind} port, {edge.dst} is a {dst_kind} port",
            )
            continue
        if edge.dst.port in incoming[edge.dst.node]:
            issue("input_already_connected", f"{path}.to", f"{edge.dst} already has an edge")
            continue
        incoming[edge.dst.node][edge.dst.port] = edge
        outgoing[edge.src.node][edge.src.port].append(edge)
        adjacency[edge.src.node].add(edge.dst.node)
        if given:
            port_types[("out", edge.src.node, edge.src.port)] = given
            port_types[("in", edge.dst.node, edge.dst.port)] = given

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
        port_types=port_types,
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
