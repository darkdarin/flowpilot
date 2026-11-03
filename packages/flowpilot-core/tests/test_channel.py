"""Edge channel semantics: backpressure, latest delivery, consumer close."""

from __future__ import annotations

import asyncio

import pytest

from flowpilot_core.runtime.channel import Channel, End, Item, Open
from flowpilot_sdk import Envelope, StreamHeader


def item(n: int) -> Item:
    return Item(Envelope("t", n, 0, n))


async def test_lossless_blocks_until_consumed() -> None:
    ch = Channel(2)
    await ch.put(Open(StreamHeader("x.y/1")))
    await ch.put(item(0))
    await ch.put(item(1))
    blocked = asyncio.create_task(ch.put(item(2)))
    await asyncio.sleep(0.01)
    assert not blocked.done()
    assert isinstance(await ch.get(), Open)
    assert not blocked.done()  # control messages do not count against capacity
    await ch.get()
    await asyncio.wait_for(blocked, 1)


async def test_latest_drops_oldest_items_but_keeps_control() -> None:
    ch = Channel(2, latest=True)
    await ch.put(Open(StreamHeader("x.y/1")))
    for n in range(5):
        await ch.put(item(n))
    await ch.put(End())
    got = [await ch.get() for _ in range(4)]
    assert isinstance(got[0], Open)
    assert [m.envelope.seq for m in got[1:3] if isinstance(m, Item)] == [3, 4]
    assert isinstance(got[3], End)
    assert ch.dropped == 3


async def test_close_unblocks_producer_and_discards() -> None:
    ch = Channel(1)
    await ch.put(item(0))
    blocked = asyncio.create_task(ch.put(item(1)))
    await asyncio.sleep(0.01)
    await ch.close()
    await asyncio.wait_for(blocked, 1)
    await ch.put(item(2))  # ignored, does not block
    assert isinstance(await ch.get(), End)


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Channel(0)
