"""Port streams: envelopes, headers and the stream interfaces a node sees."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Envelope[T]:
    """A message on an edge. Created and stripped by the runtime; nodes only see ``payload``."""

    turn_id: str
    seq: int
    t_mono_ns: int
    payload: T


@dataclass(frozen=True, slots=True)
class StreamHeader:
    """Sent once when a stream opens; ``meta`` is type-specific (e.g. the audio format)."""

    type: str
    meta: Mapping[str, Any] = field(default_factory=dict)


class StreamError(Exception):
    """The producer of a stream failed; raised to consumers while reading."""

    def __init__(self, port: str, reason: str) -> None:
        super().__init__(f"stream {port!r} failed: {reason}")
        self.port = port
        self.reason = reason


class InputStream[T](Protocol):
    """Read side of a port.

    A stream is ``absent`` when the producer ended without opening it; ``header()`` then returns
    ``None``, iteration yields nothing and ``value()`` returns ``None``.
    """

    async def header(self) -> StreamHeader | None: ...

    def __aiter__(self) -> AsyncIterator[T]: ...

    async def value(self) -> T | None:
        """For ``kind="value"`` ports: the single item, or ``None`` if absent or failed."""
        ...

    def close(self) -> None:
        """Stop consuming early; the runtime stops delivering on this edge."""
        ...


class OutputStream[T](Protocol):
    """Write side of a port. Unended outputs are ended by the runtime when ``run_turn`` returns."""

    async def open(self, meta: Mapping[str, Any] | None = None) -> None: ...

    async def send(self, payload: T) -> None:
        """Send an item; opens the stream implicitly and blocks on lossless backpressure."""
        ...

    async def end(self) -> None: ...
