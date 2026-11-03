"""FlowPilot nodes for Wyoming services."""

from __future__ import annotations

from flowpilot_sdk import Node

from .stt import WyomingStt
from .tts import WyomingTts, split_sentences

__version__ = "0.0.1"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``: node classes provided by this plugin."""
    return [WyomingStt, WyomingTts]


__all__ = ["WyomingStt", "WyomingTts", "nodes", "split_sentences"]
