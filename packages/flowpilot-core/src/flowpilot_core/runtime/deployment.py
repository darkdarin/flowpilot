"""A deployed graph: node instances and per-turn execution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from flowpilot_sdk import (
    AudioFormat,
    HostAudio,
    HostUnavailable,
    Memory,
    Node,
    SessionInfo,
    SessionState,
    TurnContext,
)

from ..registry import NodeRegistry
from ..validate import ValidatedGraph
from .channel import Channel
from .state import MemorySessionStore, SessionStateView
from .streams import RuntimeInputStream, RuntimeOutputStream
from .trace import NullTraceSink, TraceEvent, TraceSink

_LOGGER = logging.getLogger(__name__)


class NodeStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class TurnStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # some nodes failed, the turn still completed
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    status: NodeStatus
    duration_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_id: str
    status: TurnStatus
    nodes: Mapping[str, NodeOutcome] = field(default_factory=dict)


class RuntimeNodeContext:
    """``NodeContext`` handed to a node instance at start."""

    def __init__(
        self,
        node_id: str,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
        allowed: frozenset[str],
        *,
        graph_id: str = "",
        data_dir: Path | None = None,
        memory: Memory | None = None,
    ) -> None:
        self.node_id = node_id
        self.graph_id = graph_id
        self.data_dir = data_dir
        self.memory = memory
        self.config = config
        self.logger = logging.getLogger(f"flowpilot.node.{node_id}")
        self._secrets = secrets
        self._allowed = allowed

    def secret(self, name: str) -> str:
        if name not in self._allowed:
            raise KeyError(f"secret {name!r} is not referenced in the config of {self.node_id!r}")
        return self._secrets[name]


class HostCaller(Protocol):
    """The client side of a turn that can perform host operations (ADR-014)."""

    def offers(self, op: str) -> bool: ...

    async def call(
        self,
        turn_id: str,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any: ...

    def stream(
        self, turn_id: str, op: str, args: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[HostAudio]: ...


class RuntimeTurnSession:
    """``TurnSession`` for one node in one turn."""

    def __init__(
        self,
        node_id: str,
        context: TurnContext,
        session: SessionInfo,
        state: SessionStateView,
        inputs: dict[str, RuntimeInputStream],
        outputs: dict[str, RuntimeOutputStream],
        trace: TraceSink,
        *,
        host: HostCaller | None = None,
        host_ops: frozenset[str] = frozenset(),
    ) -> None:
        self.context = context
        self.session = session
        self.state: SessionState = state
        self._node_id = node_id
        self._inputs = inputs
        self._outputs = outputs
        self._trace = trace
        self._host = host
        self._host_ops = host_ops

    def _host_for(self, op: str) -> HostCaller:
        if op not in self._host_ops:
            raise RuntimeError(f"node {self._node_id!r} did not declare host operation {op!r}")
        if self._host is None:
            raise HostUnavailable(op, "this turn has no client")
        if not self._host.offers(op):
            raise HostUnavailable(op)
        self._trace.emit(
            TraceEvent(self.context.turn_id, "host.call", self._node_id, attrs={"op": op})
        )
        return self._host

    async def call_host(
        self,
        op: str,
        args: Mapping[str, Any],
        *,
        audio_in: AsyncIterable[bytes] | None = None,
        audio_format: AudioFormat | None = None,
    ) -> Any:
        if audio_in is not None and audio_format is None:
            raise ValueError("audio_format is required with audio_in")
        host = self._host_for(op)
        return await host.call(
            self.context.turn_id, op, args, audio_in=audio_in, audio_format=audio_format
        )

    def stream_host(
        self, op: str, args: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[HostAudio]:
        return self._host_for(op).stream(self.context.turn_id, op, args)

    def input(self, name: str) -> RuntimeInputStream:
        try:
            return self._inputs[name]
        except KeyError:
            raise KeyError(f"node {self._node_id!r} has no input {name!r}") from None

    def output(self, name: str) -> RuntimeOutputStream:
        try:
            return self._outputs[name]
        except KeyError:
            raise KeyError(f"node {self._node_id!r} has no output {name!r}") from None

    def trace(self, event: str, **attrs: Any) -> None:
        self._trace.emit(TraceEvent(self.context.turn_id, event, self._node_id, attrs=attrs))


class Deployment:
    """Node instances of a validated graph, able to run turns concurrently."""

    def __init__(
        self,
        graph: ValidatedGraph,
        registry: NodeRegistry,
        *,
        secrets: Mapping[str, str] | None = None,
        trace: TraceSink | None = None,
        sessions: MemorySessionStore | None = None,
        data_dir: Path | None = None,
        memory: Memory | None = None,
        taps_active: Callable[[], bool] | None = None,
    ) -> None:
        self.graph = graph
        # asked at the start of each turn: run the debug taps (someone watches live debug)?
        self._taps_active = taps_active
        # nodes get <data_dir>/nodes/<node type>/ for their files
        self._data_dir = data_dir
        self._memory = memory
        self._registry = registry
        self._secrets = secrets or {}
        self.trace: TraceSink = trace or NullTraceSink()
        self.sessions = sessions or MemorySessionStore()
        self._instances: dict[str, Node] = {}

    @property
    def registry(self) -> NodeRegistry:
        return self._registry

    def instance(self, node_id: str) -> Node:
        """A started node instance; adapters use it to feed source nodes and read sinks."""
        return self._instances[node_id]

    async def start(self) -> None:
        missing = {
            name
            for node_id, refs in self.graph.secret_refs.items()
            for name in refs
            if name not in self._secrets
        }
        if missing:
            raise KeyError(f"missing secrets: {sorted(missing)}")
        started: list[str] = []
        try:
            for node_id in self.graph.order:
                decl = self.graph.graph.node(node_id)
                node_cls = self._registry.get(decl.type)
                if node_cls is None:
                    raise LookupError(f"node type {decl.type!r} is not installed")
                instance = node_cls()
                ctx = RuntimeNodeContext(
                    node_id,
                    decl.config,
                    self._secrets,
                    self.graph.secret_refs[node_id],
                    graph_id=self.graph.graph.id,
                    data_dir=self._data_dir / "nodes" / decl.type if self._data_dir else None,
                    memory=self._memory,
                )
                await instance.start(ctx)
                self._instances[node_id] = instance
                started.append(node_id)
        except BaseException:
            await self._stop(started)
            raise

    async def stop(self) -> None:
        await self._stop(list(self._instances))

    async def _stop(self, node_ids: list[str]) -> None:
        for node_id in reversed(node_ids):
            instance = self._instances.pop(node_id, None)
            if instance is None:
                continue
            try:
                await instance.stop()
            except Exception:
                _LOGGER.exception("node %s failed to stop", node_id)

    async def run_turn(
        self, context: TurnContext, session: SessionInfo, *, host: HostCaller | None = None
    ) -> TurnResult:
        """Run every node for one turn under the turn deadline.

        ``host`` is the client of the turn that serves host operations, if any.
        """
        g = self.graph
        turn_id = context.turn_id
        # Debug taps nobody watches are left out of the turn: no channels, no task, no data.
        watched = self._taps_active is not None and self._taps_active()
        idle = set() if watched else {n for n, spec in g.specs.items() if spec.debug_tap}
        channels: dict[tuple[str, str], Channel] = {}
        for node_id, ports in g.incoming.items():
            if node_id in idle:
                continue
            spec = g.specs[node_id]
            for port_name, edge in ports.items():
                port = spec.inputs[port_name]
                channels[(node_id, port_name)] = Channel(
                    edge.buffer or port.buffer, latest=port.delivery == "latest"
                )

        sessions: dict[str, RuntimeTurnSession] = {}
        running = [n for n in g.order if n not in idle]
        for node_id in running:
            spec = g.specs[node_id]
            inputs = {
                name: RuntimeInputStream(name, port, channels.get((node_id, name)))
                for name, port in spec.inputs.items()
            }
            outputs = {
                name: RuntimeOutputStream(
                    node_id,
                    name,
                    port,
                    [
                        channels[(e.dst.node, e.dst.port)]
                        for e in g.outgoing.get(node_id, {}).get(name, ())
                        if e.dst.node not in idle
                    ],
                    turn_id,
                    on_first_item=self._first_item_tracer(turn_id, node_id),
                    on_end=self._end_tracer(turn_id, node_id),
                )
                for name, port in spec.outputs.items()
            }
            sessions[node_id] = RuntimeTurnSession(
                node_id,
                context,
                session,
                self.sessions.view(session.session_id, spec.type),
                inputs,
                outputs,
                self.trace,
                host=host,
                host_ops=spec.host_ops | spec.optional_host_ops,
            )

        outcomes: dict[str, NodeOutcome] = {}
        self.trace.emit(TraceEvent(turn_id, "turn.start"))
        remaining_s = max(0.0, (context.deadline_mono_ns - time.monotonic_ns()) / 1e9)
        timed_out = False
        try:
            async with asyncio.timeout(remaining_s), asyncio.TaskGroup() as tg:
                for node_id in running:
                    tg.create_task(
                        self._run_node(node_id, sessions[node_id], outcomes),
                        name=f"flowpilot:{turn_id}:{node_id}",
                    )
        except TimeoutError:
            timed_out = True

        if timed_out:
            status = TurnStatus.TIMEOUT
        elif all(o.status is NodeStatus.OK for o in outcomes.values()):
            status = TurnStatus.OK
        else:
            status = TurnStatus.DEGRADED
        self.trace.emit(TraceEvent(turn_id, "turn.end", attrs={"status": status.value}))
        return TurnResult(turn_id, status, dict(outcomes))

    def _first_item_tracer(self, turn_id: str, node_id: str) -> Any:
        def on_first_item(port: str) -> None:
            self.trace.emit(TraceEvent(turn_id, "port.first_item", node_id, attrs={"port": port}))

        return on_first_item

    def _end_tracer(self, turn_id: str, node_id: str) -> Any:
        def on_end(port: str, items: int, state: str) -> None:
            attrs = {"port": port, "items": items, "state": state}
            self.trace.emit(TraceEvent(turn_id, "port.end", node_id, attrs=attrs))

        return on_end

    async def _run_node(
        self, node_id: str, turn: RuntimeTurnSession, outcomes: dict[str, NodeOutcome]
    ) -> None:
        spec = self.graph.specs[node_id]
        instance = self._instances[node_id]
        turn_id = turn.context.turn_id
        started = time.monotonic()
        self.trace.emit(TraceEvent(turn_id, "node.start", node_id))
        status, error = NodeStatus.OK, None
        try:
            async with asyncio.timeout(spec.turn_timeout_s):
                await instance.run_turn(turn)
        except TimeoutError:
            status, error = NodeStatus.TIMEOUT, f"exceeded {spec.turn_timeout_s}s"
        except asyncio.CancelledError:
            status, error = NodeStatus.CANCELLED, "turn deadline"
            raise
        except Exception as err:  # noqa: BLE001 - node isolation boundary: any node bug degrades
            status, error = NodeStatus.ERROR, f"{type(err).__name__}: {err}"
            _LOGGER.warning("node %s failed in turn %s: %s", node_id, turn_id, error)
        finally:
            await asyncio.shield(self._finish_node(turn, status, error))
            outcome = NodeOutcome(status, (time.monotonic() - started) * 1000, error)
            outcomes[node_id] = outcome
            self.trace.emit(
                TraceEvent(
                    turn_id,
                    "node.end",
                    node_id,
                    attrs={
                        "status": status.value,
                        "duration_ms": round(outcome.duration_ms, 3),
                        **({"error": error[:300]} if error else {}),
                    },
                )
            )

    @staticmethod
    async def _finish_node(turn: RuntimeTurnSession, status: NodeStatus, error: str | None) -> None:
        for out in turn._outputs.values():
            if status is NodeStatus.OK:
                await out.end()
            else:
                await out.fail(error or status.value)
        for inp in turn._inputs.values():
            await inp.aclose()
