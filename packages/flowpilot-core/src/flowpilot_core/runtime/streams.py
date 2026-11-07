"""Runtime implementations of the SDK stream interfaces."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Literal

from flowpilot_sdk import Envelope, PortSpec, StreamError, StreamHeader
from flowpilot_sdk.types import type_id_of, type_kind

from .channel import Channel, End, Fail, Item, Open

State = Literal["pending", "open", "ended", "failed"]

# Keep references to fire-and-forget close tasks so they are not garbage collected mid-flight.
_BACKGROUND: set[asyncio.Task[None]] = set()
_UNSET: Any = object()


class RuntimeOutputStream:
    """Write side of a port, fanned out to every outgoing edge."""

    def __init__(
        self,
        node_id: str,
        name: str,
        spec: PortSpec,
        channels: list[Channel],
        turn_id: str,
        on_first_item: Callable[[str], None] | None = None,
        on_end: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.spec = spec
        self._channels = channels
        self._turn_id = turn_id
        self._seq = 0
        self.state: State = "pending"
        self.opened = False
        self._type = spec.type
        self._on_first_item = on_first_item
        # called once when the output ends: (port, items, "end" | "error" | "absent"); the time
        # an output ends is how long the node took with it, not when the node returned
        self._on_end = on_end

    @property
    def items(self) -> int:
        return self._seq

    @property
    def connected(self) -> bool:
        return bool(self._channels)

    async def _broadcast(self, msg: Open | Item | End | Fail) -> None:
        for ch in self._channels:
            await ch.put(msg)

    async def open(
        self, meta: Mapping[str, Any] | None = None, *, type_id: str | None = None
    ) -> None:
        if type_id is not None and type_id not in self.spec.types:
            raise TypeError(f"output {self.node_id}.{self.name} does not carry {type_id}")
        if self.spec.is_union and type_id is None:
            # which of its types it carries: given, or known from the first payload
            raise RuntimeError(
                f"output {self.node_id}.{self.name} takes several types: give type_id or send"
            )
        await self._open(type_id or self.spec.type, meta)

    async def _open(self, type_id: str, meta: Mapping[str, Any] | None = None) -> None:
        if self.state != "pending":
            raise RuntimeError(f"output {self.node_id}.{self.name} is already {self.state}")
        self.state = "open"
        self.opened = True
        self._type = type_id
        await self._broadcast(Open(StreamHeader(type_id, dict(meta or {}))))

    async def send(self, payload: Any) -> None:
        sent = type_id_of(type(payload))
        if sent not in self.spec.types or (self.state == "open" and sent != self._type):
            raise TypeError(
                f"output {self.node_id}.{self.name} expects "
                f"{self._type if self.state == 'open' else self.spec.type}, "
                f"got {type(payload).__name__}"
            )
        if self.state == "pending":
            await self._open(sent)
        elif self.state != "open":
            raise RuntimeError(f"output {self.node_id}.{self.name} is already {self.state}")
        if self._seq == 0 and self._on_first_item is not None:
            self._on_first_item(self.name)
        envelope = Envelope(self._turn_id, self._seq, time.monotonic_ns(), payload)
        self._seq += 1
        await self._broadcast(Item(envelope))
        if (type_kind(sent) if self.spec.is_union else self.spec.kind) == "value":
            await self.end()

    async def end(self) -> None:
        if self.state in ("ended", "failed"):
            return
        self.state = "ended"
        self._ended("end" if self.opened else "absent")
        await self._broadcast(End())

    async def fail(self, reason: str) -> None:
        if self.state in ("ended", "failed"):
            return
        self.state = "failed"
        self._ended("error")
        await self._broadcast(Fail(reason))

    def _ended(self, state: str) -> None:
        if self._on_end is not None:
            self._on_end(self.name, self._seq, state)


class RuntimeInputStream:
    """Read side of a port. An unconnected input is absent from the start."""

    def __init__(self, name: str, spec: PortSpec, channel: Channel | None) -> None:
        self.name = name
        self.spec = spec
        self._channel = channel
        self._header: StreamHeader | None = None
        self._state: State = "pending" if channel is not None else "ended"
        self._fail_reason = ""
        self._iterating = False
        self._value: Any = _UNSET

    @property
    def connected(self) -> bool:
        return self._channel is not None

    async def _next(self) -> Open | Item | End | Fail:
        assert self._channel is not None
        return await self._channel.get()

    async def header(self) -> StreamHeader | None:
        if self._state == "pending":
            msg = await self._next()
            if isinstance(msg, Open):
                self._header, self._state = msg.header, "open"
            elif isinstance(msg, Fail):
                self._state, self._fail_reason = "failed", msg.reason
            else:  # End before Open: absent; an Item cannot arrive before Open
                self._state = "ended"
        if self._state == "failed":
            raise StreamError(self.name, self._fail_reason)
        return self._header

    def __aiter__(self) -> AsyncIterator[Any]:
        if self._iterating:
            raise RuntimeError(f"input {self.name!r} is already being iterated")
        self._iterating = True
        return self._iter()

    async def _iter(self) -> AsyncIterator[Any]:
        if await self.header() is None:
            return
        while self._state == "open":
            msg = await self._next()
            if isinstance(msg, Item):
                yield msg.envelope.payload
            elif isinstance(msg, Fail):
                self._state, self._fail_reason = "failed", msg.reason
                raise StreamError(self.name, msg.reason)
            else:
                self._state = "ended"

    async def value(self) -> Any | None:
        kind = self.spec.kind
        if self.spec.is_union:  # the kind of what is connected
            try:
                header = await self.header()
            except StreamError:
                return None
            if header is None:
                return None
            kind = type_kind(header.type)
        if kind != "value":
            raise TypeError(f"input {self.name!r} is a stream port; iterate it instead")
        if self._value is _UNSET:
            self._value = None
            try:
                async for payload in self:
                    self._value = payload
                    break
            except StreamError:
                self._value = None
        return self._value

    def close(self) -> None:
        if self._channel is not None and not self._channel.closed:
            self._state = "ended"
            task = asyncio.get_running_loop().create_task(self._channel.close())
            _BACKGROUND.add(task)
            task.add_done_callback(_BACKGROUND.discard)

    async def aclose(self) -> None:
        """Runtime side: close when the node is done so producers never block on it."""
        if self._channel is not None:
            await self._channel.close()
