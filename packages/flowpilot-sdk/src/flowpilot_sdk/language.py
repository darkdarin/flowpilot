"""The language of a text: in a stream's header, and guessed from the text's alphabet.

A text stream (``text.delta/1``) may say its language in its header: ``meta["language"]``, a code
such as ``ru`` or ``en-US``. An agent opens its answer with the language it answers in; a node that
passes text on keeps the header; a text-to-speech node speaks that language when its own settings
do not fix one. Read it with :func:`stream_language`.

:func:`language_by_script` guesses the language of a text by its alphabet, without models or
dictionaries: Cyrillic, Latin, Greek, Arabic, Hebrew, Devanagari, Thai, Georgian, Armenian, Korean,
Japanese and Chinese. Languages sharing an alphabet are told apart only by the preferred languages
(the language of the question, of the turn): Latin text is English unless a preferred language is
written in Latin, Cyrillic text is Russian unless a preferred one is Cyrillic.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .streams import StreamHeader

LANGUAGE_META = "language"

# languages by alphabet; the first is the one taken when no preferred language uses it
_SCRIPT_LANGUAGES: dict[str, tuple[str, ...]] = {
    "latin": (
        "en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk", "sl", "hr", "bs", "ro", "hu",
        "fi", "sv", "da", "no", "nb", "nn", "is", "et", "lv", "lt", "tr", "az", "uz", "id", "ms",
        "vi", "ca", "eu", "gl", "ga", "cy", "sq", "sw", "tl", "af", "mt", "lb",
    ),
    "cyrillic": ("ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "mn", "tg"),
    "greek": ("el",),
    "arabic": ("ar", "fa", "ur", "ps"),
    "hebrew": ("he", "yi"),
    "devanagari": ("hi", "mr", "ne"),
    "thai": ("th",),
    "georgian": ("ka",),
    "armenian": ("hy",),
    "hangul": ("ko",),
    "kana": ("ja",),
    "han": ("zh", "ja"),
}  # fmt: skip

# Unicode name prefixes of letters, by alphabet
_NAME_PREFIXES: tuple[tuple[str, str], ...] = (
    ("LATIN", "latin"),
    ("CYRILLIC", "cyrillic"),
    ("GREEK", "greek"),
    ("ARABIC", "arabic"),
    ("HEBREW", "hebrew"),
    ("DEVANAGARI", "devanagari"),
    ("THAI", "thai"),
    ("GEORGIAN", "georgian"),
    ("ARMENIAN", "armenian"),
    ("HANGUL", "hangul"),
    ("HIRAGANA", "kana"),
    ("KATAKANA", "kana"),
    ("CJK", "han"),
)


def language_base(language: str | None) -> str | None:
    """``en`` of ``en-US`` or ``en_us``; ``None`` for nothing."""
    if not language:
        return None
    return language.replace("_", "-").split("-")[0].lower() or None


def stream_language(header: StreamHeader | Mapping[str, Any] | None) -> str | None:
    """The language a text stream says in its header (the header or its ``meta``)."""
    meta = header.meta if isinstance(header, StreamHeader) else header
    value = meta.get(LANGUAGE_META) if meta else None
    return str(value) if value else None


def _script(char: str) -> str | None:
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    for prefix, script in _NAME_PREFIXES:
        if name.startswith(prefix):
            return script
    return None


def text_script(text: str) -> str | None:
    """The alphabet most letters of ``text`` are written in, or ``None`` without letters.
    Japanese kana anywhere makes Chinese characters Japanese."""
    counts = Counter(s for c in text if (s := _script(c)) is not None)
    if not counts:
        return None
    if counts.get("kana") and counts.get("han"):
        counts["kana"] += counts.pop("han")
    return counts.most_common(1)[0][0]


def language_by_script(text: str, prefer: Iterable[str | None] = ()) -> str | None:
    """The language of ``text`` by its alphabet: the first preferred language written in it,
    else the usual language of that alphabet; ``None`` for a text without letters."""
    script = text_script(text)
    if script is None:
        return None
    languages = _SCRIPT_LANGUAGES[script]
    for wanted in prefer:
        base = language_base(wanted)
        if base is not None and base in languages:
            return wanted
    return languages[0]
