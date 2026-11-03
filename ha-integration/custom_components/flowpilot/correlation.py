"""Link the STT and conversation stages of one Assist pipeline run (ADR-012).

Home Assistant awaits the speech-to-text entity and the conversation agent in the same asyncio task
of a pipeline run, so a context variable set by the STT entity is visible to the conversation
entity of that run and to nothing else. This relies on Home Assistant internals; it is verified
for 2026.10 and 2026.11 and must be re-checked for every new release.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import TurnHandle

CURRENT_TURN: ContextVar[TurnHandle | None] = ContextVar("flowpilot_current_turn", default=None)


def take_voice_turn(conversation_id: str | None) -> TurnHandle | None:
    """Return the voice turn started by our STT entity in this pipeline run, if it matches.

    The conversation id must match as a consistency check; on mismatch the turn is not used
    (fail closed: the request is handled as a new text turn without voice identity).
    """
    handle = CURRENT_TURN.get()
    if handle is None:
        return None
    CURRENT_TURN.set(None)
    if handle.conversation_id != conversation_id:
        return None
    return handle
