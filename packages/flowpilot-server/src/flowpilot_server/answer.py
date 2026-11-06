"""Debugging: a stand-in agent that answers with the text from its config, at once or word by word.

With its ``utterance`` input connected (to speech-to-text, or the Client input for typed turns) it
answers each utterance; ``{text}`` in the answer is replaced with what was said, so ``echo: {text}``
repeats it. Not connected, it answers at the start of every turn: to try text-to-speech or the
client with a known text. Word by word it streams like an agent that is still typing.
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

TYPE = "debug.answer"
PLACEHOLDER = "{text}"


class FixedAnswer:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Fixed answer",
        category="debug",
        inputs={
            "utterance": PortSpec(
                "text.utterance/1",
                kind="value",
                required=False,
                description=(
                    "Optional: what to answer, like an agent's input; no answer in a turn without "
                    "it. Not connected: the answer goes out at the start of every turn."
                ),
            )
        },
        outputs={
            "delta": PortSpec(
                "text.delta/1",
                description="The answer as a stream, at once or word by word: for text-to-speech.",
            ),
            "response": PortSpec(
                "agent.response/1",
                kind="value",
                description="The whole answer, for Client output.",
            ),
        },
        config_schema={
            "type": "object",
            "properties": {
                "text": {
                    "title": "Answer",
                    "description": "What the node answers; {text} is replaced with the utterance.",
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
                "word_delay_ms": {
                    "title": "Pause between words, ms",
                    "description": "Word by word only; 150 when not set.",
                    "type": "integer",
                    "default": 150,
                    "minimum": 0,
                    "maximum": 5000,
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        turn_timeout_s=120.0,
        description=(
            "For debugging: stands in for an agent and answers with the text from its settings, "
            "at once or word by word; {text} in it repeats what was said."
        ),
    )

    def __init__(self) -> None:
        self._text = ""
        self._words = False
        self._delay_s = 0.15

    async def start(self, ctx: NodeContext) -> None:
        self._text = str(ctx.config["text"])
        self._words = ctx.config.get("mode", "whole") == "words"
        self._delay_s = int(ctx.config.get("word_delay_ms", 150)) / 1000

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        said = turn.input("utterance")
        language = turn.context.language
        heard = ""
        if await said.header() is not None:  # connected and present: answer it
            utterance: Utterance | None = await said.value()
            if utterance is None:
                return
            heard = utterance.text
            language = utterance.language or language
        elif said.connected:
            return  # connected but absent in this turn: nothing to answer
        text = self._text.replace(PLACEHOLDER, heard)
        delta = turn.output("delta")
        if self._words:
            words = text.split(" ")
            for i, word in enumerate(words):
                if i:
                    await asyncio.sleep(self._delay_s)
                await delta.send(TextDelta(word if i == 0 else f" {word}"))
        else:
            await delta.send(TextDelta(text))
        await delta.end()
        await turn.output("response").send(Response(text=text, language=language))
