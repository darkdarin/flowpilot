"""The language of a text: in a stream's header and by its alphabet."""

from __future__ import annotations

import pytest

from flowpilot_sdk import StreamHeader
from flowpilot_sdk.language import language_base, language_by_script, stream_language, text_script


@pytest.mark.parametrize(
    ("text", "prefer", "language"),
    [
        ("My name is Jarvis.", (), "en"),
        ("My name is Jarvis.", ("ru", None), "en"),  # Russian is not written in Latin
        ("Меня зовут Джарвис.", ("ru-RU",), "ru-RU"),  # the preferred one, as it was given
        ("Меня зовут Джарвис.", (), "ru"),
        ("Ich heiße Jarvis.", ("ru", "de"), "de"),  # Latin: the preferred Latin language
        ("Мене звати Джарвіс.", ("uk",), "uk"),
        ("Включи Queen, пожалуйста", ("ru",), "ru"),  # mostly Cyrillic
        ("Γεια σου", (), "el"),
        ("こんにちは世界", (), "ja"),  # kana makes the Chinese characters Japanese
        ("你好世界", (), "zh"),
        ("안녕하세요", (), "ko"),
        ("12:30 — 42!", ("ru",), None),  # no letters
        ("", (), None),
    ],
)
def test_language_by_script(text: str, prefer: tuple[str | None, ...], language: str) -> None:
    assert language_by_script(text, prefer) == language


def test_text_script() -> None:
    assert text_script("Hello, мир!") == "latin"
    assert text_script("...") is None


def test_stream_language() -> None:
    assert stream_language(StreamHeader("text.delta/1", {"language": "en"})) == "en"
    assert stream_language(StreamHeader("text.delta/1")) is None
    assert stream_language({"language": "ru"}) == "ru"
    assert stream_language(None) is None


def test_language_base() -> None:
    assert language_base("en_US") == "en"
    assert language_base("ru-RU") == "ru"
    assert language_base("") is None
