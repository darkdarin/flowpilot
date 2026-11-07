"""FlowPilot nodes for Wyoming services."""

from __future__ import annotations

from flowpilot_sdk import Node
from flowpilot_sdk.helpers import split_sentences

from .stt import WyomingStt
from .tts import WyomingTts

__version__ = "0.1.15"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``: node classes provided by this plugin."""
    return [WyomingStt, WyomingTts]


__all__ = ["WyomingStt", "WyomingTts", "nodes", "split_sentences"]
