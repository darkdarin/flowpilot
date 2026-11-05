"""Link the stages of one Assist pipeline run to one FlowPilot turn (ADR-012, ADR-017).

Home Assistant awaits the speech-to-text entity and the conversation agent in the same asyncio task
of a pipeline run, so a context variable set by the STT entity is visible to the conversation
entity of that run and to nothing else. Text-to-speech is synthesized in a task the pipeline
starts after (or, streaming, while) the agent answers, which copies that context: a variable set by
the conversation entity is visible to our text-to-speech entity of the same run. This relies on
Home Assistant internals; it is verified for 2026.10 and 2026.11 and must be re-checked for every
new release (tests/test_tts.py).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from homeassistant.helpers import chat_session

if TYPE_CHECKING:
    from .client import TurnHandle

CURRENT_TURN: ContextVar[TurnHandle | None] = ContextVar("flowpilot_current_turn", default=None)
# The turn whose answer the conversation entity gave in this run: its speech is what to play.
CURRENT_SPEECH: ContextVar[TurnHandle | None] = ContextVar("flowpilot_speech", default=None)


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


def take_speech() -> TurnHandle | None:
    """The turn whose speech our text-to-speech entity should play in this pipeline run, once.

    The dialogue of the run must be the turn's (fail closed: anything else is synthesized as plain
    text). A value set inside the synthesis task does not reach the pipeline, so "once" is a flag
    on the turn.
    """
    handle = CURRENT_SPEECH.get()
    if handle is None or handle.speech_taken:
        return None
    handle.speech_taken = True
    session = chat_session.current_session.get()
    if session is None or session.conversation_id != handle.conversation_id:
        return None
    return handle
