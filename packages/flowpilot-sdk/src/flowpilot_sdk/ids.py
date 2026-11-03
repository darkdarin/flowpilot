"""Identifier helpers: ULIDs for turns, sessions and traces."""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a new ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32.

    ULIDs sort lexicographically by creation time, like the ids Home Assistant uses.
    """
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= ts < 1 << 48:
        raise ValueError("timestamp out of ULID range")
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
