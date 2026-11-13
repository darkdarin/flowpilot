"""Small helpers shared by node implementations (speech synthesis in particular)."""

from __future__ import annotations

import re
import time
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


class SpeechTiming:
    """How long a text-to-speech node took, measured the same way in every one of them: from the
    first text it got (not from its start, which waits for the agent, nor from its connection) to
    its first and its last audio, and how much speech that was.

    Call :meth:`text` for each piece of text and :meth:`audio` for each piece of audio;
    :meth:`attrs` gives ``first_audio_ms``, ``done_ms``, ``seconds`` and ``realtime`` (seconds of
    speech per second of synthesis) for the node's trace event.
    """

    def __init__(self) -> None:
        self._text_at: float | None = None
        self._first_at: float | None = None
        self._last_at: float | None = None
        self._seconds = 0.0

    def text(self) -> None:
        if self._text_at is None:
            self._text_at = time.monotonic()

    def audio(self, samples: int, sample_rate: int) -> None:
        now = time.monotonic()
        if self._first_at is None:
            self._first_at = now
        self._last_at = now
        self._seconds += samples / sample_rate if sample_rate else 0.0

    def attrs(self) -> dict[str, Any]:
        def since_text(at: float | None) -> int | None:
            if at is None or self._text_at is None:
                return None
            return max(0, round(1000 * (at - self._text_at)))

        done_ms = since_text(self._last_at)
        return {
            "first_audio_ms": since_text(self._first_at),
            "done_ms": done_ms,
            "seconds": round(self._seconds, 2),
            "realtime": round(self._seconds / (done_ms / 1000), 1) if done_ms else None,
        }


class SpeechWriter:
    """Writes speech from several synthesis requests to one audio output.

    Opens the output with the first format, rejects format changes and keeps sample offsets
    continuous across requests. With ``timing``, every chunk is counted in it.
    """

    def __init__(self, out: OutputStream[Any], timing: SpeechTiming | None = None) -> None:
        self._out = out
        self.format: AudioFormat | None = None
        self._offset = 0
        self._timing = timing

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
        samples = len(pcm) // self.format.bytes_per_sample
        self._offset += samples
        if self._timing is not None:
            self._timing.audio(samples // self.format.channels, self.format.sample_rate)
