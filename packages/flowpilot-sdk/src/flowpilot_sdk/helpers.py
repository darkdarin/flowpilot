"""Small helpers shared by node implementations (speech synthesis in particular)."""

from __future__ import annotations

import re
from typing import Any

from .payloads import AudioChunk, AudioFormat
from .streams import OutputStream

# End of a sentence: terminal punctuation (optionally closing quotes/brackets) followed by space,
# or a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]»]*\s+|\n+")
MIN_SENTENCE_CHARS = 12


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """Split complete sentences off ``buffer``; return them and the incomplete rest.

    Very short sentences are merged with the next one so a TTS gets meaningful pieces.
    """
    sentences: list[str] = []
    start = 0
    pending = ""
    for match in _SENTENCE_END.finditer(buffer):
        piece = pending + buffer[start : match.end()]
        start = match.end()
        if len(piece.strip()) < MIN_SENTENCE_CHARS:
            pending = piece
            continue
        sentences.append(piece.strip())
        pending = ""
    return sentences, pending + buffer[start:]


class SpeechWriter:
    """Writes speech from several synthesis requests to one audio output.

    Opens the output with the first format, rejects format changes and keeps sample offsets
    continuous across requests.
    """

    def __init__(self, out: OutputStream[Any]) -> None:
        self._out = out
        self.format: AudioFormat | None = None
        self._offset = 0

    async def start(self, fmt: AudioFormat) -> None:
        if fmt.encoding != "s16le":
            raise ValueError(f"unsupported speech encoding {fmt.encoding!r}, need s16le")
        if self.format is None:
            self.format = fmt
            await self._out.open(fmt.as_meta())
        elif fmt != self.format:
            raise ValueError(f"speech format changed mid-answer: {self.format} -> {fmt}")

    async def chunk(self, pcm: bytes) -> None:
        if self.format is None:
            raise ValueError("speech audio before its format")
        await self._out.send(AudioChunk(pcm, self._offset))
        self._offset += len(pcm) // self.format.bytes_per_sample
