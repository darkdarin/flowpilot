"""Bounded per-edge channel carrying stream control messages and items."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from flowpilot_sdk import Envelope, StreamHeader


@dataclass(frozen=True, slots=True)
class Open:
    header: StreamHeader


@dataclass(frozen=True, slots=True)
class Item:
    envelope: Envelope[Any]


@dataclass(frozen=True, slots=True)
class End:
    pass


@dataclass(frozen=True, slots=True)
class Fail:
    reason: str


Message = Open | Item | End | Fail


class Channel:
    """One edge. Control messages are never dropped; items obey capacity and delivery mode.

    ``lossless``: the producer waits while the channel holds ``capacity`` items.
    ``latest``: the oldest item is dropped to make room.
    Once the consumer closes the channel, everything sent is discarded without blocking.
    """

    def __init__(self, capacity: int, *, latest: bool = False) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._queue: deque[Message] = deque()
        self._items = 0
        self._capacity = capacity
        self._latest = latest
        self._closed = False
        self._cond = asyncio.Condition()
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def put(self, msg: Message) -> None:
        async with self._cond:
            if self._closed:
                return
            if isinstance(msg, Item):
                if self._latest:
                    while self._items >= self._capacity:
                        self._drop_oldest_item()
                else:
                    await self._cond.wait_for(lambda: self._closed or self._items < self._capacity)
                    if self._closed:
                        return
                self._items += 1
            self._queue.append(msg)
            self._cond.notify_all()

    async def get(self) -> Message:
        async with self._cond:
            await self._cond.wait_for(lambda: bool(self._queue) or self._closed)
            if not self._queue:
                return End()  # closed by the consumer itself
            msg = self._queue.popleft()
            if isinstance(msg, Item):
                self._items -= 1
            self._cond.notify_all()
            return msg

    async def close(self) -> None:
        """Consumer side: stop receiving; unblock and discard everything."""
        async with self._cond:
            self._closed = True
            self._queue.clear()
            self._items = 0
            self._cond.notify_all()

    def _drop_oldest_item(self) -> None:
        for i, msg in enumerate(self._queue):
            if isinstance(msg, Item):
                del self._queue[i]
                self._items -= 1
                self.dropped += 1
                return
