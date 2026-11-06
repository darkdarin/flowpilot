"""16-bit PCM helpers: levels in dBFS and gain with saturation."""

from __future__ import annotations

import math
import sys
from array import array

FULL_SCALE = 32768.0
SILENCE_DBFS = -120.0  # what an all-zero block measures


def samples(pcm: bytes) -> array[int]:
    """Little-endian 16-bit PCM as signed integers (a trailing odd byte is dropped)."""
    out = array("h")
    out.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        out.byteswap()
    return out


def to_bytes(values: array[int]) -> bytes:
    if sys.byteorder != "little":
        values = array("h", values)
        values.byteswap()
    return values.tobytes()


def db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def dbfs(level: float) -> float:
    """A level in samples (RMS or peak) in dB relative to full scale."""
    return 20.0 * math.log10(level / FULL_SCALE) if level > 0 else SILENCE_DBFS


def rms(values: array[int]) -> float:
    return math.sqrt(math.fsum(v * v for v in values) / len(values)) if values else 0.0


def peak(values: array[int]) -> int:
    return max((abs(v) for v in values), default=0)


def apply_gain(
    values: array[int], start: float, end: float | None = None
) -> tuple[array[int], int]:
    """Multiply by a gain that moves linearly from ``start`` to ``end`` across the block.

    Returns the result and how many samples had to be clipped to the 16-bit range.
    """
    end = start if end is None else end
    n = len(values)
    step = (end - start) / n if n else 0.0
    out = array("h", bytes(2 * n))
    clipped = 0
    g = start
    for i, v in enumerate(values):
        s = round(v * g)
        if s > 32767:
            s, clipped = 32767, clipped + 1
        elif s < -32768:
            s, clipped = -32768, clipped + 1
        out[i] = s
        g += step
    return out, clipped
