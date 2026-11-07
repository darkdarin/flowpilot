"""FlowPilot runtime: graph model, validation and execution."""

from .errors import GraphError, GraphIssue
from .graph import (
    SCHEMA_VERSION,
    EdgeDecl,
    Graph,
    GraphSettings,
    NodeDecl,
    PortRef,
    parse_graph,
)
from .registry import NodeRegistry
from .validate import GrantPolicy, ValidatedGraph, validate_graph
from .versions import satisfies

__version__ = "0.1.22"

__all__ = [
    "SCHEMA_VERSION",
    "EdgeDecl",
    "GrantPolicy",
    "Graph",
    "GraphError",
    "GraphIssue",
    "GraphSettings",
    "NodeDecl",
    "NodeRegistry",
    "PortRef",
    "ValidatedGraph",
    "parse_graph",
    "satisfies",
    "validate_graph",
]
