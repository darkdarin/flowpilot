"""MCP servers the runtime is connected to, as a node sees them (ADR-025).

The runtime keeps the connections (one per server, for every graph), signs in with OAuth or a
token, and caches each server's tools. A node gets them through ``NodeContext.mcp``: the servers
and their state, their tools with JSON Schema parameters, and a way to call one. Nodes never see
tokens, and this module does not depend on an MCP library.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

TOOL_PREFIX = "mcp__"

ServerState = Literal["ready", "connecting", "needs_auth", "error", "disabled"]


@dataclass(frozen=True, slots=True)
class McpServer:
    id: str
    name: str  # short latin name, part of the agent's tool names: mcp__<name>__<tool>
    title: str  # what people call it
    state: ServerState
    error: str | None = None  # why it is not ready, for people


@dataclass(frozen=True, slots=True)
class McpTool:
    server: str  # McpServer.id
    name: str  # as the server names it
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)  # JSON Schema of the arguments
    title: str = ""
    # MCP annotations are hints from the server, not guarantees (ADR-025: rights in FlowPilot)
    read_only: bool = False
    destructive: bool = True


@dataclass(frozen=True, slots=True)
class McpResult:
    text: str  # what the tool returned, as text for a model (untrusted data)
    is_error: bool = False  # the tool reported a failure


class McpUnavailable(Exception):
    """The server is not connected (or not signed in), or it did not answer in time."""


class McpServers(Protocol):
    """The runtime's MCP servers; read them per turn: people add servers and sign in while
    graphs run, and servers change their tools."""

    def servers(self) -> list[McpServer]: ...

    def tools(self, servers: Iterable[str] | None = None) -> list[McpTool]:
        """The enabled tools of ready servers (of ``servers`` ids, or all of them)."""
        ...

    async def call(
        self, server: str, tool: str, arguments: Mapping[str, Any], *, timeout_s: float
    ) -> McpResult:
        """Call one tool; ``McpUnavailable`` when the server cannot be reached in time."""
        ...
