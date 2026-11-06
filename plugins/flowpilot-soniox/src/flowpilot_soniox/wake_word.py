"""Remove the wake word (and anything before it) from the start of a transcript.

Some satellites send the audio buffered before the command, including the wake word itself.
Soniox recognizes the whole word reliably, so cutting it from the text is more robust than
cutting a fixed amount of audio.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

MIN_SIMILARITY = 0.8
_NON_WORD = re.compile(r"[^\w]+")


@dataclass
class Token:
    """Final Soniox token. Tokens are word pieces; a new word starts with a space."""

    text: str
    start_ms: int = 0


def _normalize(word: str) -> str:
    return _NON_WORD.sub("", word.lower().replace("ё", "е"))


def _similar(word: str, expected: str) -> bool:
    return SequenceMatcher(None, _normalize(word), _normalize(expected)).ratio() >= MIN_SIMILARITY


def _words(tokens: list[Token]) -> list[tuple[str, int]]:
    """Group tokens into (word, start_ms); punctuation stays attached to its word."""
    words: list[tuple[str, int]] = []
    for token in tokens:
        if not words or token.text[:1].isspace():
            words.append((token.text, token.start_ms))
        else:
            text, start_ms = words[-1]
            words[-1] = (text + token.text, start_ms)
    return words


def strip_wake_word(tokens: list[Token], wake_words: list[str], window_ms: int) -> tuple[str, bool]:
    """Return the text without the first wake word that starts within `window_ms`.

    Everything recognized before the wake word is dropped as well. The second value tells
    whether a wake word was found.
    """
    words = _words(tokens)
    for wake_word in wake_words:
        expected = wake_word.split()
        if not expected:
            continue
        for index in range(len(words) - len(expected) + 1):
            if words[index][1] >= window_ms:
                break
            candidate = words[index : index + len(expected)]
            if all(_similar(word, exp) for (word, _), exp in zip(candidate, expected, strict=True)):
                rest = "".join(word for word, _ in words[index + len(expected) :])
                rest = rest.lstrip(" ,.!?;:-—").strip()
                return rest[:1].upper() + rest[1:], True
    return "".join(word for word, _ in words).strip(), False
