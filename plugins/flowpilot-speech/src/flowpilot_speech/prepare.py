"""Russian text made ready for a Russian voice: what it would misread is spelled in words.

In this order: the replacements (built-in and the owner's, whole words, case ignored); times
(12:30); numbers with their unit or currency, the unit agreeing with the number (5 °C → пять
градусов, $3 → три доллара); English words by pronunciation (Queen → кв+ин); acronyms letter by
letter (TV → ти-ви, МЧС → эм-че-эс). A ``+`` in a replacement marks the stress; without stress
marks they are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from num2words import num2words  # type: ignore[import-untyped]

from .english import STRESS, cyrillic, is_latin

# Built-in replacements, the owner's come first and win. Keys are matched as whole words.
DEFAULT_REPLACEMENTS: dict[str, str] = {
    "Wi-Fi": "вай-ф+ай",
    "WiFi": "вай-ф+ай",
    "iPhone": "айф+он",
    "iPad": "айп+эд",
    "MacBook": "макб+ук",
    "YouTube": "ют+уб",
    "Google": "г+угл",
    "Spotify": "спот+ифай",
    "Bluetooth": "блют+уз",
    "Netflix": "н+етфликс",
    "Zigbee": "з+игби",
    "Home Assistant": "х+оум ас+истант",
    "OK": "ок+ей",
    "the": "зе",
    "т.е.": "то +есть",
    "т. е.": "то +есть",
    "т.д.": "так д+алее",
    "т. д.": "так д+алее",
    "т.п.": "т+ому под+обное",
    "т. п.": "т+ому под+обное",
}

# a unit after a number: its word (agreeing with the number)
UNITS: dict[str, str] = {
    "%": "процент",
    "°C": "градус",
    "°С": "градус",  # a Cyrillic С
    "°": "градус",
    "км/ч": "километр в час",
    "м/с": "метр в секунду",
    "км": "километр",
    "м": "метр",
    "см": "сантиметр",
    "мм": "миллиметр",
    "кг": "килограмм",
    "мин": "минута",
    "сек": "секунда",
    "ч": "час",
    "руб.": "рубль",
    "руб": "рубль",
    "₽": "рубль",
    "кВт·ч": "киловатт-час",
    "кВт": "киловатт",
    "Вт": "ватт",
    "мл": "миллилитр",
    "л": "литр",
    "ГБ": "гигабайт",
    "МБ": "мегабайт",
}
# a currency before a number: its word
CURRENCIES: dict[str, str] = {"$": "доллар", "€": "евро", "₽": "рубль", "£": "фунт"}
RUSSIAN_LETTERS = {
    "а": "а", "б": "бэ", "в": "вэ", "г": "гэ", "д": "дэ", "е": "е", "ж": "жэ", "з": "зэ",
    "и": "и", "й": "и кр+аткое", "к": "ка", "л": "эль", "м": "эм", "н": "эн", "о": "о",
    "п": "пэ", "р": "эр", "с": "эс", "т": "тэ", "у": "у", "ф": "эф", "х": "ха", "ц": "цэ",
    "ч": "че", "ш": "ша", "щ": "ща", "ы": "ы", "э": "э", "ю": "ю", "я": "я",
}  # fmt: skip
RUSSIAN_VOWELS = set("аеёиоуыэюя")

_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’]*(?:-[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’]*)*")


def _units_pattern() -> str:
    names = sorted(UNITS, key=len, reverse=True)
    return "|".join(re.escape(n).replace("°", r"°\s?") for n in names)


_NUMBER = re.compile(
    r"(?P<cur>[$€₽£])?"
    r"(?:(?<![\w.,])(?P<sign>[+\-−]))?"
    r"(?P<num>\d+(?:[.,]\d+)?)"
    r"(?:-(?P<ord>ый|ой|ий|ая|ое|ые|ых|ым|ыми|ого|ому|ом|ой|го|му|й|я|е|х|м|ю))?"
    r"(?:\s?(?P<unit>" + _units_pattern() + r")(?![A-Za-zА-Яа-яЁё]))?"
    r"(?![A-Za-zА-Яа-яЁё])"
)
# an ordinal's ending: its grammemes (1-й → первый, 2-го → второго, 3-я → третья)
ORDINAL_FORMS: dict[str, frozenset[str]] = {
    "й": frozenset({"masc", "nomn"}), "ый": frozenset({"masc", "nomn"}),
    "ой": frozenset({"masc", "nomn"}), "ий": frozenset({"masc", "nomn"}),
    "я": frozenset({"femn", "nomn"}), "ая": frozenset({"femn", "nomn"}),
    "е": frozenset({"neut", "nomn"}), "ое": frozenset({"neut", "nomn"}),
    "ые": frozenset({"plur", "nomn"}),
    "го": frozenset({"masc", "gent"}), "ого": frozenset({"masc", "gent"}),
    "му": frozenset({"masc", "datv"}), "ому": frozenset({"masc", "datv"}),
    "м": frozenset({"masc", "loct"}), "ом": frozenset({"masc", "loct"}),
    "ю": frozenset({"femn", "accs"}),
    "х": frozenset({"plur", "loct"}), "ых": frozenset({"plur", "loct"}),
    "ым": frozenset({"masc", "ablt"}), "ыми": frozenset({"plur", "ablt"}),
}  # fmt: skip


@dataclass
class Options:
    stress: bool = False
    english: bool = True
    acronyms: bool = True
    numbers: bool = True
    replacements: Mapping[str, str] = field(default_factory=dict)


def parse_replacements(lines: Iterable[str]) -> dict[str, str]:
    """``word = как сказать`` lines; others are skipped."""
    out: dict[str, str] = {}
    for line in lines:
        key, sep, value = str(line).partition("=")
        if sep and key.strip() and value.strip():
            out[key.strip()] = value.strip()
    return out


class Preparer:
    """Prepares Russian text; ``pronounce`` is the CMU dictionary (word → ARPAbet), ``morph`` a
    ``pymorphy3.MorphAnalyzer`` (units agree with numbers, ordinals take their endings)."""

    def __init__(self, options: Options, pronounce: Mapping[str, list[str]], morph: Any) -> None:
        self.options = options
        self._pronounce = pronounce
        self._morph = morph
        merged = {**DEFAULT_REPLACEMENTS, **options.replacements}
        # the owner's spelling of a key wins over a built-in one in another case
        replacements: dict[str, str] = {}
        for key, value in merged.items():
            replacements[key.casefold()] = value
        self._replacements = replacements
        keys = sorted(replacements, key=len, reverse=True)
        self._replace_re = (
            re.compile(
                r"(?<![\w])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w])",
                re.IGNORECASE,
            )
            if keys
            else None
        )
        # the most words a replacement key has: a stream holds back that many minus one
        self.key_words = max((len(k.split()) for k in keys), default=1)

    def __call__(self, text: str) -> str:
        o = self.options
        if self._replace_re is not None:
            text = self._replace_re.sub(lambda m: self._replacements[m[1].casefold()], text)
        if o.numbers:
            text = _TIME.sub(self._time, text)
            text = _NUMBER.sub(self._number, text)
        if o.english or o.acronyms:
            text = _WORD.sub(self._word, text)
        if not o.stress:
            text = text.replace(STRESS, "")
        return text

    # --- numbers ---------------------------------------------------------------------------------

    @staticmethod
    def _time(m: re.Match[str]) -> str:
        hours, minutes = int(m[1]), m[2]
        said_minutes = (
            "ноль ноль"
            if minutes == "00"
            else (f"ноль {_words(int(minutes))}" if minutes[0] == "0" else _words(int(minutes)))
        )
        return f"{_words(hours)} {said_minutes}"

    def _number(self, m: re.Match[str]) -> str:
        raw = m["num"]
        whole = "," not in raw and "." not in raw
        value: Any = int(raw) if whole else float(raw.replace(",", "."))
        parts: list[str] = []
        if sign := m["sign"]:
            parts.append("плюс" if sign == "+" else "минус")
        if (ending := m["ord"]) and whole:
            parts.append(self._ordinal(value, ORDINAL_FORMS[ending]))
        else:
            parts.append(_words(value))
        count = value if whole else None
        unit = m["unit"]
        if unit is not None:
            name = UNITS[re.sub(r"\s", "", unit)]
            parts.append(self._agree_phrase(name, count))
        if cur := m["cur"]:
            parts.append(self._agree_phrase(CURRENCIES[cur], count))
        return " ".join(parts)

    def _agree_phrase(self, phrase: str, count: int | None) -> str:
        """The unit agrees with the number: its first word (километров в час), or the last part
        of a hyphenated one (киловатт-часов)."""
        first, _, rest = phrase.partition(" ")
        if "-" in first and not rest:
            head, _, last = first.rpartition("-")
            return f"{head}-{self._agree(last, count)}"
        return f"{self._agree(first, count)} {rest}".strip()

    def _agree(self, word: str, count: int | None) -> str:
        """``градус`` for 1, ``градуса`` for 2, ``градусов`` for 5 and fractions' genitive."""
        parsed = self._morph.parse(word)[0]
        form = (
            parsed.make_agree_with_number(abs(count))
            if count is not None
            else parsed.inflect({"gent", "sing"})
        )
        return str(form.word) if form is not None else word

    def _ordinal(self, value: int, grammemes: frozenset[str]) -> str:
        words = str(num2words(value, lang="ru", to="ordinal")).split()
        parsed = self._morph.parse(words[-1])[0]
        form = parsed.inflect(set(grammemes))
        if form is not None:
            words[-1] = str(form.word)
        return " ".join(words)

    # --- words -----------------------------------------------------------------------------------

    def _word(self, m: re.Match[str]) -> str:
        word = m[0]
        o = self.options
        if is_latin(word):
            if not o.english:
                return word
            return "-".join(
                _like(part, cyrillic(part, self._pronounce, stress=o.stress, acronyms=o.acronyms))
                for part in word.split("-")
            )
        if o.acronyms and _russian_acronym(word):
            return "-".join(RUSSIAN_LETTERS.get(c, c) for c in word.lower())
        return word


def _like(word: str, spelled: str) -> str:
    """A capitalized word stays capitalized (an acronym's letters do not)."""
    if not word[:1].isupper() or word.isupper():
        return spelled
    at = 1 if spelled.startswith(STRESS) else 0
    return spelled[:at] + spelled[at : at + 1].upper() + spelled[at + 1 :]


def _words(value: int | float) -> str:
    return str(num2words(value, lang="ru"))


def _russian_acronym(word: str) -> bool:
    """Capitals without a vowel (МЧС, ФСБ): read letter by letter. With vowels (МИД, ВУЗ) the
    acronym is read as a word."""
    letters = [c for c in word if c.isalpha()]
    return (
        len(letters) >= 2
        and all(c.isupper() for c in letters)
        and not any(c.lower() in RUSSIAN_VOWELS for c in letters)
    )
