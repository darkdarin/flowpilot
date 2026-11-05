"""Turn tracing. Events carry no payload content (audio, text); nodes add safe attributes.

The one exception is a debug tap that shows text (``debug.probe``): it runs only while someone
watches the graph in live debug, and only on the wires it is connected to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class TraceEvent:
    turn_id: str
    event: str
    node_id: str | None = None
    t_mono_ns: int = field(default_factory=time.monotonic_ns)
    attrs: dict[str, Any] = field(default_factory=dict)


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class ListTraceSink:
    """Collects events in memory (tests, debug UI buffer)."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)

    def named(self, event: str, node_id: str | None = None) -> list[TraceEvent]:
        return [
            e for e in self.events if e.event == event and (node_id is None or e.node_id == node_id)
        ]


class NullTraceSink:
    def emit(self, event: TraceEvent) -> None:
        return None
