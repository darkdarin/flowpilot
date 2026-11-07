"""Names Home Assistant knows, found in a phrase in any grammatical form.

Home Assistant's sentence matching compares names literally: an area called "Гостиная" is not
found in "включи свет в гостиной". This puts each name back as it is written: words of the
phrase and of the names are compared by their dictionary forms (Russian morphology with
pymorphy3; other languages compare words as they are), and a run of words that matches a name
becomes the name: "включи свет в гостиной" → "включи свет в Гостиная".
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

# languages with a morphological dictionary; others match word for word
MORPHOLOGY = {"ru"}


def words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.casefold().replace("ё", "е")).split()


@cache
def _analyzer() -> Any:
    import pymorphy3  # type: ignore[import-untyped]  # noqa: PLC0415 - loaded on first use

    return pymorphy3.MorphAnalyzer(lang="ru")


def warm_up() -> None:
    """Load the dictionary (a fraction of a second) ahead of the first phrase."""
    _analyzer()


@cache
def _forms(word: str, language: str) -> frozenset[str]:
    """The dictionary forms a word may have; the word itself when there is no dictionary."""
    if language not in MORPHOLOGY:
        return frozenset({word})
    return frozenset({word, *(p.normal_form.replace("ё", "е") for p in _analyzer().parse(word))})


class NameMatcher:
    def __init__(self, names: list[str], language: str | None) -> None:
        self.language = (language or "").split("-")[0].casefold()
        self._names: list[tuple[list[frozenset[str]], str]] = []
        for name in names:
            parts = words(name)
            if parts:
                self._names.append(([_forms(w, self.language) for w in parts], name))
        self._names.sort(key=lambda entry: -len(entry[0]))  # the longest name wins

    def canonical(self, text: str) -> str:
        """``text`` with every name in it written as Home Assistant has it."""
        tokens = words(text)
        out: list[str] = []
        i = 0
        while i < len(tokens):
            for forms, name in self._names:
                n = len(forms)
                if i + n <= len(tokens) and all(
                    forms[k] & _forms(tokens[i + k], self.language) for k in range(n)
                ):
                    out.append(name)
                    i += n
                    break
            else:
                out.append(tokens[i])
                i += 1
        return " ".join(out)
