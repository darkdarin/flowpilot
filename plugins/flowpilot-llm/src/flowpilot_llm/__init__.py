"""FlowPilot LLM agent: any OpenAI-compatible chat API, streamed."""

from __future__ import annotations

from flowpilot_sdk import Node

from .agent import LlmAgent, LlmError

__version__ = "0.1.21"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [LlmAgent]


__all__ = ["LlmAgent", "LlmError", "nodes"]
