"""Debugging: a node that sends text from its config, at once or word by word.

Without its ``after`` input it speaks at the start of every turn (to try text-to-speech or the
client with a known text); with ``after`` connected (say, to the speech-to-text utterance) it waits
for that value and stands in for an agent with a fixed answer. Word by word it streams like an
agent that is still typing, with a pause between words.

The Play button on the node in the editor starts a turn of its own (``TRIGGER_KEY``); set ``when``
to ``play`` and the node speaks only then, staying silent in the household's real turns.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from flowpilot_sdk import (
    ENUM_TITLES_KEY,
    NodeContext,
    NodeSpec,
    PortSpec,
    Response,
    TextDelta,
    TurnSession,
    Utterance,
    node_translations,
)

from .protocol import TRIGGER_KEY

TYPE = "debug.text"


class FixedText:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Fixed text",
        category="debug",
        inputs={
            "after": PortSpec(
                "text.utterance/1",
                kind="value",
                required=False,
                description=(
                    "Optional: send the text after this value arrives (for example the "
                    "speech-to-text result); nothing is sent in a turn without it. Not connected: "
                    "the text goes out at the start of every turn."
                ),
            )
        },
        outputs={
            "delta": PortSpec(
                "text.delta/1",
                description="The text as a stream, at once or word by word: for text-to-speech.",
            ),
            "utterance": PortSpec(
                "text.utterance/1",
                kind="value",
                description="The whole text as an utterance, as if the user said it.",
            ),
            "response": PortSpec(
                "agent.response/1",
                kind="value",
                description="The whole text as an agent's answer, for Client output.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "text": {
                    "title": "Text",
                    "description": "What the node sends.",
                    "type": "string",
                    "minLength": 1,
                },
                "mode": {
                    "title": "How to send",
                    "description": "At once, or word by word like an agent that is still typing.",
                    "enum": ["whole", "words"],
                    ENUM_TITLES_KEY: {"whole": "At once", "words": "Word by word"},
                    "default": "whole",
                },
                "when": {
                    "title": "When to send",
                    "description": (
                        "In every turn, or only in turns started by the Play button on this node "
                        "in the editor."
                    ),
                    "enum": ["turn", "play"],
                    ENUM_TITLES_KEY: {"turn": "Every turn", "play": "Only by Play"},
                    "default": "turn",
                },
                "word_delay_ms": {
                    "title": "Pause between words, ms",
                    "description": "Word by word only; 150 when not set.",
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5000,
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        turn_timeout_s=120.0,
        description=(
            "For debugging: sends the text from its settings, at once or word by word, to "
            "text-to-speech, an agent or the client."
        ),
    )

    def __init__(self) -> None:
        self._text = ""
        self._words = False
        self._delay_s = 0.15
        self._node_id = ""
        self._only_play = False

    async def start(self, ctx: NodeContext) -> None:
        self._text = str(ctx.config["text"])
        self._words = ctx.config.get("mode", "whole") == "words"
        self._delay_s = int(ctx.config.get("word_delay_ms", 150)) / 1000
        self._node_id = ctx.node_id
        self._only_play = ctx.config.get("when", "turn") == "play"

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        after = turn.input("after")
        language = turn.context.language
        played = turn.context.external.get(TRIGGER_KEY) == self._node_id
        if self._only_play and not played:
            return
        # the Play button speaks at once; otherwise wait for `after` when it is connected
        if not played and await after.header() is not None:  # connected and present
            utterance: Utterance | None = await after.value()
            if utterance is None:
                return
            language = utterance.language or language
        elif after.connected and not played:
            return  # connected but absent in this turn: nothing to answer
        delta = turn.output("delta")
        if self._words:
            words = self._text.split(" ")
            for i, word in enumerate(words):
                if i:
                    await asyncio.sleep(self._delay_s)
                await delta.send(TextDelta(word if i == 0 else f" {word}"))
        else:
            await delta.send(TextDelta(self._text))
        await delta.end()
        await turn.output("utterance").send(Utterance(self._text, language))
        await turn.output("response").send(Response(text=self._text, language=language))
