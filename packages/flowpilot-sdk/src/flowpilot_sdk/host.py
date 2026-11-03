"""Host operations: work a node asks the connected client (e.g. Home Assistant) to do (ADR-014)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from .payloads import AudioFormat


class HostUnavailable(Exception):
    """The client of this turn does not provide the requested operation (or there is no client).

    Nodes treat this like any failed input: fail closed.
    """

    def __init__(self, op: str, reason: str = "not offered by the client") -> None:
        super().__init__(f"host operation {op!r} unavailable: {reason}")
        self.op = op
        self.reason = reason


class HostError(Exception):
    """The client accepted the operation but it failed."""

    def __init__(self, op: str, message: str) -> None:
        super().__init__(f"host operation {op!r} failed: {message}")
        self.op = op
        self.message = message


class HostAudio(Protocol):
    """Audio a host operation streams back (e.g. synthesized speech), then its result."""

    @property
    def format(self) -> AudioFormat | None:
        """Format of the audio; ``None`` if the operation returned no audio."""
        ...

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Raw PCM chunks as the host produces them."""
        ...

    async def result(self) -> Any:
        """The operation's result; raises ``HostError`` if it failed."""
        ...
