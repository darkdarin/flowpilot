"""FlowPilot text for speech: what a Russian voice would misread, spelled in words."""

from __future__ import annotations

from flowpilot_sdk import Node

from .node import SpeechText
from .prepare import Options, Preparer, parse_replacements

__version__ = "0.1.38"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [SpeechText]


__all__ = ["Options", "Preparer", "SpeechText", "nodes", "parse_replacements"]
