"""Voices people enrolled, for telling who speaks (``NodeContext.voices``).

The runtime keeps a voice profile per person (embeddings of their speech, never the audio) and
compares a turn's speech with them. A match says who it likely is; it is a convenience, not proof
(a recording can fool it), so identity from a voice alone stays at ``Assurance.LOW``.
"""

from __future__ import annotations

from typing import Protocol

from .payloads import SpeakerMatch


class Voices(Protocol):
    async def identify(self, pcm: bytes, sample_rate: int) -> SpeakerMatch:
        """Who of the enrolled people this speech (s16le mono) is: ``MATCHED`` with their user
        id when the closest profile is close enough and clearly closer than the next, else
        ``UNKNOWN`` (or ``ERROR`` when there is no model or no profile)."""
        ...
