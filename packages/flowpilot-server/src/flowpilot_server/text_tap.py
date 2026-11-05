"""Debugging: a tap that shows the text on the wires it is connected to, in live debug.

Connect any of its inputs next to the regular consumer of a text output. At the end of each input
the node reports what came through as a ``tap.text`` trace event (port, text, items; partial
transcripts are counted), which the editor shows on the node for the selected turn. The text stays
in the runtime's memory with the rest of the live debug history; nothing is written to disk.

A debug tap: it only runs while someone watches the graph in live debug, so the household's words
reach the trace only when you look.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from flowpilot_sdk import (
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    StreamError,
    TurnSession,
    node_translations,
)

TYPE = "debug.text_tap"
MAX_CHARS = 2000


def _cut(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


class TextTap:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type=TYPE,
        translations=node_translations(__package__, TYPE),
        version="1.0.0",
        title="Show text",
        category="debug",
        debug_tap=True,
        inputs={
            "transcript": PortSpec(
                "text.transcript/1",
                required=False,
                description="Recognized speech: the final text and how many partial ones came.",
            ),
            "utterance": PortSpec(
                "text.utterance/1", kind="value", required=False, description="An utterance."
            ),
            "delta": PortSpec(
                "text.delta/1",
                required=False,
                description="A text stream, joined into one text, with the number of pieces.",
            ),
            "response": PortSpec(
                "agent.response/1",
                kind="value",
                required=False,
                description="An agent's answer.",
            ),
        },
        turn_timeout_s=660.0,
        description=(
            "For debugging: shows the text that reaches it on the node in live debug, for the "
            "selected turn. Works only while live debug is on; the text is not saved."
        ),
    )

    async def start(self, ctx: NodeContext) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        await asyncio.gather(
            *(
                self._watch(turn, name, turn.input(name))
                for name in self.spec.inputs
                if turn.input(name).connected
            )
        )

    async def _watch(self, turn: TurnSession, name: str, stream: InputStream[Any]) -> None:
        if await stream.header() is None:
            return
        attrs: dict[str, Any] = {"port": name}
        try:
            if self.spec.inputs[name].kind == "value":
                value = await stream.value()
                if value is None:
                    return
                attrs |= {"text": _cut(value.text), "items": 1}
            elif name == "transcript":
                final, last, partials, items = "", "", 0, 0
                async for item in stream:
                    items += 1
                    last = item.text
                    if item.final:
                        final = item.text
                    else:
                        partials += 1
                attrs |= {"text": _cut(final or last), "items": items, "partials": partials}
            else:
                pieces = [item.text async for item in stream]
                attrs |= {"text": _cut("".join(pieces)), "items": len(pieces)}
        except StreamError as err:
            attrs |= {"error": err.reason}
        turn.trace("tap.text", **attrs)
