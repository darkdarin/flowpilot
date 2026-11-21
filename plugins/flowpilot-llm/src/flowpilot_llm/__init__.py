"""FlowPilot LLM agents: any OpenAI-compatible chat API, streamed; a speech-to-speech one."""

from __future__ import annotations

from flowpilot_sdk import Node

from .agent import LlmAgent, LlmError
from .voice import VoiceAgent

__version__ = "0.1.43"


def nodes() -> list[type[Node]]:
    """Entry point ``flowpilot.nodes``."""
    return [LlmAgent, VoiceAgent]


__all__ = ["LlmAgent", "LlmError", "VoiceAgent", "nodes"]
