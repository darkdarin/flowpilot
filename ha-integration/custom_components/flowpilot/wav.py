"""Incremental WAV parsing for streamed TTS results (header first, then raw PCM)."""

from __future__ import annotations

import struct
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass


class WavError(Exception):
    """The stream is not a 16-bit PCM WAV."""


@dataclass(frozen=True, slots=True)
class WavFormat:
    sample_rate: int
    channels: int
    bits: int

    def as_meta(self) -> dict[str, object]:
        return {"encoding": "s16le", "sample_rate": self.sample_rate, "channels": self.channels}


async def open_wav(chunks: AsyncIterable[bytes]) -> tuple[WavFormat, AsyncIterator[bytes]]:
    """Read the WAV header from ``chunks``; return its format and the PCM that follows.

    Streamed WAV often has an unknown data length (0 or 0xFFFFFFFF); everything after the
    ``data`` chunk header is treated as PCM. Extra chunks such as ``LIST`` are skipped.
    """
    it = aiter(chunks)
    buffer = b""

    async def need(n: int) -> None:
        nonlocal buffer
        while len(buffer) < n:
            try:
                buffer += await anext(it)
            except StopAsyncIteration:
                raise WavError("stream ended inside the WAV header") from None

    await need(12)
    if buffer[:4] != b"RIFF" or buffer[8:12] != b"WAVE":
        raise WavError("not a RIFF/WAVE stream")
    pos = 12
    fmt: WavFormat | None = None
    while True:
        await need(pos + 8)
        chunk_id, size = buffer[pos : pos + 4], struct.unpack_from("<I", buffer, pos + 4)[0]
        if chunk_id == b"data":
            if fmt is None:
                raise WavError("data before fmt chunk")
            pos += 8
            break
        await need(pos + 8 + size)
        if chunk_id == b"fmt ":
            audio_format, channels, rate = struct.unpack_from("<HHI", buffer, pos + 8)
            bits = struct.unpack_from("<H", buffer, pos + 22)[0]
            if audio_format != 1 or bits != 16:
                raise WavError(f"need 16-bit PCM, got format {audio_format} with {bits} bits")
            fmt = WavFormat(rate, channels, bits)
        pos += 8 + size + (size & 1)  # chunks are word aligned

    rest = buffer[pos:]

    async def pcm() -> AsyncIterator[bytes]:
        if rest:
            yield rest
        async for chunk in it:
            yield chunk

    return fmt, pcm()
