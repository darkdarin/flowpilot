"""FlowPilot Soniox nodes: real-time speech-to-text and text-to-speech."""

from __future__ import annotations

from flowpilot_sdk import Node

from .common import SonioxError
from .stt import SonioxStt
from .tts import SonioxTts

__version__ = "0.1.28"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [SonioxStt, SonioxTts]


__all__ = ["SonioxError", "SonioxStt", "SonioxTts", "nodes"]
