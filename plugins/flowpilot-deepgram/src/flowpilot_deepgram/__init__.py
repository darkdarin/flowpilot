"""FlowPilot Deepgram nodes: Flux real-time speech-to-text and text-to-speech."""

from __future__ import annotations

from flowpilot_sdk import Node

from .common import DeepgramError
from .stt import DeepgramStt
from .tts import DeepgramTts

__version__ = "0.1.35"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [DeepgramStt, DeepgramTts]


__all__ = ["DeepgramError", "DeepgramStt", "DeepgramTts", "nodes"]
