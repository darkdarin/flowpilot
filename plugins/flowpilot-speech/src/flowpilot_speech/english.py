"""English words in Cyrillic, the way a Russian voice should say them.

A word the CMU Pronouncing Dictionary knows is spelled from its phonemes (ARPAbet) with the
primary stress marked; an unknown word is spelled by simple letter rules. Acronyms are spelled
letter by letter, as English letters are named ("TV" → "ти-ви").
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# a stress mark goes right before the stressed vowel (Silero's "+")
STRESS = "+"

VOWELS = {
    "AA": "а", "AE": "э", "AH": "а", "AO": "о", "AW": "ау", "AY": "ай", "EH": "э", "ER": "ер",
    "EY": "эй", "IH": "и", "IY": "и", "OW": "оу", "OY": "ой", "UH": "у", "UW": "у",
}  # fmt: skip
CONSONANTS = {
    "B": "б", "CH": "ч", "D": "д", "DH": "з", "F": "ф", "G": "г", "HH": "х", "JH": "дж", "K": "к",
    "L": "л", "M": "м", "N": "н", "NG": "нг", "P": "п", "R": "р", "S": "с", "SH": "ш", "T": "т",
    "TH": "с", "V": "в", "W": "в", "Y": "й", "Z": "з", "ZH": "ж",
}  # fmt: skip
# "Y" and a vowel: one Russian letter (you → ю, yes → е)
IOTATED = {"UW": "ю", "UH": "ю", "AA": "я", "AE": "я", "EH": "е", "AO": "ё", "OW": "ё"}
# EH after a consonant is written е, as in Russian loanwords (net → нет); AE keeps э (cat → кэт)
SOFT_AFTER_CONSONANT = {"EH": "е"}

LETTER_NAMES = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч",
    "i": "ай", "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи",
    "q": "кью", "r": "ар", "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл-ю", "x": "экс",
    "y": "уай", "z": "зед",
}  # fmt: skip
# letter rules for words the dictionary does not know, longest first
_RULES: list[tuple[str, str]] = sorted(
    {
        "tion": "шн", "sion": "жн", "ture": "чер", "ough": "оу", "igh": "ай", "sch": "ск",
        "tch": "ч", "sh": "ш", "ch": "ч", "th": "т", "ph": "ф", "ck": "к", "qu": "кв", "wh": "в",
        "kn": "н", "wr": "р", "ee": "и", "ea": "и", "oo": "у", "ou": "ау", "ow": "оу", "ai": "эй",
        "ay": "эй", "ey": "эй", "oi": "ой", "oy": "ой", "au": "о", "aw": "о", "ie": "и",
        "ce": "се", "ci": "си", "cy": "си", "ge": "дже", "gi": "джи",
        "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
        "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
        "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
    }.items(),
    key=lambda rule: -len(rule[0]),
)  # fmt: skip
_LATIN = re.compile(r"[A-Za-z]")
_VOWEL_LETTERS = set("aeiouy")


def is_latin(word: str) -> bool:
    return bool(_LATIN.search(word))


def from_phonemes(phonemes: list[str], *, stress: bool) -> str:
    """ARPAbet phonemes (``K W IY1 N``) in Cyrillic (``кв+ин``)."""
    out: list[str] = []
    i = 0
    while i < len(phonemes):
        phone = phonemes[i]
        base, digit = phone.rstrip("012"), phone[len(phone.rstrip("012")) :]
        mark = STRESS if stress and digit == "1" else ""
        if base in VOWELS:
            letters = VOWELS[base]
            if out and out[-1][-1:] not in "аеёиоуыэюяй" and base in SOFT_AFTER_CONSONANT:
                letters = SOFT_AFTER_CONSONANT[base]
            out.append(mark + letters)
        elif base == "Y" and i + 1 < len(phonemes):
            nxt = phonemes[i + 1]
            nbase = nxt.rstrip("012")
            if nbase in IOTATED:
                nmark = STRESS if stress and nxt.endswith("1") else ""
                out.append(nmark + IOTATED[nbase])
                i += 2
                continue
            out.append(CONSONANTS[base])
        else:
            out.append(CONSONANTS.get(base, ""))
        i += 1
    return "".join(out)


def by_letters(word: str) -> str:
    """An unknown English word by simple letter rules, no stress."""
    lower = word.lower()
    out: list[str] = []
    i = 0
    while i < len(lower):
        if not lower[i].isalpha():
            out.append(lower[i])
            i += 1
            continue
        if lower[i] == "e" and i == len(lower) - 1 and i > 1:
            i += 1  # a silent final e (make, home)
            continue
        for pattern, letters in _RULES:
            if lower.startswith(pattern, i):
                out.append(letters)
                i += len(pattern)
                break
        else:
            i += 1
    return "".join(out)


def spell_letters(word: str) -> str:
    """Letter by letter as English letters are named: TV → ти-ви."""
    return "-".join(LETTER_NAMES[c] for c in word.lower() if c in LETTER_NAMES)


def is_acronym(word: str) -> bool:
    """Capitals only, up to five, or without a vowel at all (TV, USB, BBC)."""
    letters = [c for c in word if c.isalpha()]
    if len(letters) < 2 or not all(c.isupper() for c in letters):
        return False
    return len(letters) <= 3 or not any(c.lower() in _VOWEL_LETTERS for c in letters)


def cyrillic(
    word: str, pronounce: Mapping[str, list[str]], *, stress: bool, acronyms: bool = True
) -> str:
    """One English word (letters, maybe an apostrophe) in Cyrillic."""
    if acronyms and is_acronym(word):
        return spell_letters(word)
    phonemes = pronounce.get(word.lower())
    if phonemes:
        return from_phonemes(phonemes, stress=stress)
    return by_letters(word)
