"""Turn and session context, owned by the runtime and read-only for nodes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .payloads import Principal


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Where the turn came from: ``kind`` such as ``ha.assist``, ``ha.conversation``, ``ws``."""

    kind: str
    adapter_id: str


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Opaque device identifiers of the source system (Home Assistant ids in Assist mode)."""

    device_id: str | None = None
    satellite_id: str | None = None
    area: str | None = None


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Everything known about the turn before the graph runs."""

    turn_id: str
    session_id: str
    started_at: datetime
    deadline_mono_ns: int
    source: SourceInfo
    trace_id: str
    initial_principal: Principal
    device: DeviceInfo | None = None
    language: str | None = None
    external: Mapping[str, str] = field(default_factory=dict)
    # The client sends audio: a spoken turn (Assist voice, a satellite), not a typed one (chat,
    # Telegram through Assist). Graphs use it to skip what only voice needs (text-to-speech).
    voice: bool = False


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """The dialogue a turn belongs to."""

    session_id: str
    created_at: datetime
    turn_index: int
    external: Mapping[str, str] = field(default_factory=dict)


class SessionState(Protocol):
    """Key-value state that outlives a turn, namespaced per node type by the runtime."""

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...
