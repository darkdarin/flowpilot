"""``speech.text``: text made ready for a Russian voice, between an agent and text-to-speech.

Streams: whole words go on as soon as they have come (a number waits for the next word, its
unit; a phrase of the replacements, for its last word). The header goes on as it came, the text's
language included. Text in another language than Russian goes on unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import re
from typing import Any, ClassVar

from flowpilot_sdk import (
    InputStream,
    NodeContext,
    NodeSpec,
    PortSpec,
    TextDelta,
    TurnSession,
    node_translations,
)
from flowpilot_sdk.language import language_base, stream_language

from .prepare import CURRENCIES, Options, Preparer, parse_replacements

_SPACE = re.compile(r"\s+")
_NUMBERISH = re.compile(r"\d|^[+\-−]$")


@functools.cache
def resources() -> tuple[dict[str, list[str]], Any]:
    """The CMU dictionary (first pronunciation of each word) and the Russian morphology; loaded
    once per process, they take a second and some tens of MB."""
    import cmudict  # noqa: PLC0415
    import pymorphy3  # type: ignore[import-untyped]  # noqa: PLC0415

    pronounce = {word: prons[0] for word, prons in cmudict.dict().items() if prons}
    return pronounce, pymorphy3.MorphAnalyzer(lang="ru")


def split_point(buffer: str, hold_words: int) -> int:
    """Where the text ready to go ends: after the last whole word, holding back ``hold_words``
    words more, and a number (its unit may follow)."""
    ends = [m.end() for m in _SPACE.finditer(buffer)]
    starts = [0, *ends]
    i = len(ends) - 1 - hold_words
    while i >= 0:
        word = buffer[starts[i] : ends[i]].strip()
        if not (_NUMBERISH.search(word) or word in CURRENCIES):
            return ends[i]
        i -= 1
    return 0


class SpeechText:
    spec: ClassVar[NodeSpec] = NodeSpec(
        type="speech.text",
        translations=node_translations(__package__, "speech.text"),
        version="1.0.0",
        title="Text for speech",
        category="speech",
        inputs={"text": PortSpec("text.delta/1", description="Text to speak, as it comes.")},
        outputs={
            "text": PortSpec(
                "text.delta/1", description="The same text, spelled for a Russian voice."
            )
        },
        config_schema={
            "type": "object",
            "properties": {
                "english": {
                    "title": "English words",
                    "description": (
                        "Spell English words as they are pronounced (Queen → квин), from a "
                        "pronouncing dictionary, unknown ones by their letters."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "acronyms": {
                    "title": "Acronyms",
                    "description": (
                        "Spell acronyms letter by letter: TV → ти-ви, МЧС → эм-че-эс (ones with "
                        "vowels, like МИД, are read as words)."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "numbers": {
                    "title": "Numbers",
                    "description": (
                        "Numbers, times, ordinals, units and currencies in words: 5 °C → пять "
                        "градусов, 12:30 → двенадцать тридцать."
                    ),
                    "type": "boolean",
                    "default": True,
                },
                "stress": {
                    "title": "Stress marks",
                    "description": (
                        "Mark the stress of spelled words with + before the vowel (кв+ин), as "
                        "Silero voices read it. Off for voices that would read the mark."
                    ),
                    "type": "boolean",
                    "default": False,
                },
                "replacements": {
                    "title": "Replacements",
                    "description": (
                        "How to say words and phrases, one per line: word = как сказать (a + "
                        "marks the stress). Whole words, case ignored; they win over the "
                        "built-in ones (Wi-Fi, iPhone, YouTube…)."
                    ),
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
        description=(
            "Spells for a Russian voice what it would misread: English words by pronunciation, "
            "acronyms, numbers with units, and your own replacements. Put it between an agent "
            "and text-to-speech; text in other languages goes on unchanged."
        ),
    )

    def __init__(self) -> None:
        self._preparer: Preparer | None = None

    async def start(self, ctx: NodeContext) -> None:
        cfg = ctx.config
        options = Options(
            stress=bool(cfg.get("stress", False)),
            english=bool(cfg.get("english", True)),
            acronyms=bool(cfg.get("acronyms", True)),
            numbers=bool(cfg.get("numbers", True)),
            replacements=parse_replacements(cfg.get("replacements") or []),
        )
        pronounce, morph = await asyncio.to_thread(resources)
        self._preparer = Preparer(options, pronounce, morph)

    async def stop(self) -> None:
        return None

    async def run_turn(self, turn: TurnSession) -> None:
        text: InputStream[Any] = turn.input("text")
        header = await text.header()
        if header is None:
            return
        out = turn.output("text")
        await out.open(header.meta)
        language = language_base(stream_language(header) or turn.context.language)
        prepare = self._preparer
        if language != "ru" or prepare is None:
            async for delta in text:
                await out.send(delta)
            await out.end()
            turn.trace("speech.text", language=language, prepared=False)
            return
        buffer, chars_in, chars_out = "", 0, 0
        hold = prepare.key_words - 1
        async for delta in text:
            buffer += delta.text
            chars_in += len(delta.text)
            if cut := split_point(buffer, hold):
                ready = prepare(buffer[:cut])
                buffer = buffer[cut:]
                chars_out += len(ready)
                await out.send(TextDelta(ready))
        if buffer:
            ready = prepare(buffer)
            chars_out += len(ready)
            await out.send(TextDelta(ready))
        await out.end()
        turn.trace("speech.text", language=language, prepared=True, chars=chars_in, out=chars_out)
