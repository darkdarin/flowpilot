"""FlowPilot audio processing: gain and automatic gain control."""

from __future__ import annotations

from flowpilot_sdk import Node

from .nodes import AutoGain, Gain, GainControl

__version__ = "0.1.9"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [Gain, AutoGain]


__all__ = ["AutoGain", "Gain", "GainControl", "nodes"]
