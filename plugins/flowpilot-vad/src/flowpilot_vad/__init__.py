"""FlowPilot voice activity detection."""

from __future__ import annotations

from flowpilot_sdk import Node

from .node import VadSegmenter
from .segmenter import CommandSegmenter, EnergyDetector, FrameBuffer, MicroDetector, Outcome

__version__ = "0.1.15"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [VadSegmenter]


__all__ = [
    "CommandSegmenter",
    "EnergyDetector",
    "FrameBuffer",
    "MicroDetector",
    "Outcome",
    "VadSegmenter",
    "nodes",
]
