"""The session's dialogue, shared by every node that answers: what was said and answered.

Nodes that answer (an LLM agent, Home Assistant's quick commands) add their exchanges to it, so
an agent knows what happened in the turns another node answered ("turn the light off" answered by
a quick command, then "turn it back on" for the agent). It lives in ``TurnSession.shared``; each
reader decides how far back to look.
"""

from __future__ import annotations

import time
from typing import Any

from .context import SessionState

KEY = "dialogue"
# The longest dialogue a session keeps; readers send fewer.
MAX_EXCHANGES = 50
# How long a quiet dialogue is kept at all; readers forget it sooner by their own settings.
KEEP_S = 30 * 24 * 3600.0


async def exchanges(state: SessionState) -> list[dict[str, Any]]:
    """Every kept exchange, oldest first: ``{"user", "assistant", "by", "at"}``."""
    value = await state.get(KEY)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


async def add(state: SessionState, user: str, assistant: str, *, by: str) -> None:
    """Add an exchange answered by node ``by``."""
    kept = await exchanges(state)
    kept.append({"user": user, "assistant": assistant, "by": by, "at": time.time()})
    await state.set(KEY, kept[-MAX_EXCHANGES:], ttl_s=KEEP_S)


def recent(kept: list[dict[str, Any]], *, turns: int, quiet_s: float) -> list[dict[str, Any]]:
    """The last ``turns`` exchanges, or none when the dialogue was quiet longer than
    ``quiet_s``: after a long pause the next question starts afresh."""
    if not kept or turns <= 0 or time.time() - float(kept[-1].get("at") or 0) > quiet_s:
        return []
    return kept[-turns:]
