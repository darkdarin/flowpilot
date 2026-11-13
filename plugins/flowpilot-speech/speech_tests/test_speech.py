"""Text for speech: the spelling rules and the streaming node."""

from __future__ import annotations

from typing import Any

import pytest

from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import TextDelta
from flowpilot_speech import Options, Preparer, SpeechText, parse_replacements
from flowpilot_speech.english import by_letters, from_phonemes, is_acronym, spell_letters
from flowpilot_speech.node import resources, split_point


def preparer(**options: Any) -> Preparer:
    pronounce, morph = resources()
    return Preparer(Options(**options), pronounce, morph)


# --- English -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phonemes", "spelled"),
    [
        ("K W IY1 N", "кв+ин"),
        ("N EH1 T F L IH0 K S", "н+етфликс"),  # EH after a consonant: е
        ("K AE1 T", "к+эт"),  # AE keeps э
        ("Y UW1", "+ю"),  # Y and a vowel: one letter
        ("Y EH1 S", "+ес"),
        ("HH OW1 M", "х+оум"),
    ],
)
def test_phonemes_in_cyrillic(phonemes: str, spelled: str) -> None:
    assert from_phonemes(phonemes.split(), stress=True) == spelled
    assert from_phonemes(phonemes.split(), stress=False) == spelled.replace("+", "")


def test_unknown_words_by_letters() -> None:
    assert by_letters("Zorbix") == "зорбикс"
    assert by_letters("shake") == "шак"  # a silent final e
    assert by_letters("cinema") == "синема"


def test_acronyms() -> None:
    assert is_acronym("TV") and is_acronym("USB") and is_acronym("HTML")
    assert not is_acronym("NASA") and not is_acronym("Tv") and not is_acronym("A")
    assert spell_letters("TV") == "ти-ви"


# --- the whole text ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Включаю Queen.", "Включаю Кв+ин."),
        ("Включи TV и Wi-Fi", "Включи ти-ви и вай-ф+ай"),
        ("МЧС предупреждает, МИД молчит", "эм-че-эс предупреждает, МИД молчит"),
        ("На улице -5 °C", "На улице минус пять градусов"),
        ("Сейчас +1°C", "Сейчас плюс один градус"),
        ("Влажность 85%, 2 процента", "Влажность восемьдесят пять процентов, два процента"),
        ("Скидка 1,5%", "Скидка одна целая пять десятых процента"),
        ("Ветер 3 м/с", "Ветер три метра в секунду"),
        ("Расход 12 кВт·ч", "Расход двенадцать киловатт-часов"),
        ("Это $3 или 250 руб.", "Это три доллара или двести пятьдесят рублей"),
        ("Будильник на 7:05 и 12:00", "Будильник на семь ноль пять и двенадцать ноль ноль"),
        ("Это 2-го ноября, 3-я серия", "Это второго ноября, третья серия"),
        ("Счёт 5-6", "Счёт пять-шесть"),
        ("и т.д.", "и так д+алее"),
    ],
)
def test_prepares_russian_text(text: str, spoken: str) -> None:
    assert preparer(stress=True)(text) == spoken


def test_without_stress_marks_and_switched_off_parts() -> None:
    assert preparer()("Включаю Queen") == "Включаю Квин"
    assert preparer(english=False)("Включаю Queen") == "Включаю Queen"
    assert preparer(numbers=False)("Будет 5 градусов") == "Будет 5 градусов"
    assert preparer(acronyms=False)("МЧС") == "МЧС"


def test_own_replacements_win() -> None:
    own = parse_replacements(["Алиса = Ал+иса", "wi-fi = вифи", "broken line", "= nothing"])
    assert own == {"Алиса": "Ал+иса", "wi-fi": "вифи"}
    prepare = preparer(stress=True, replacements=own | {"Living Room": "г+остиная"})
    assert prepare("Wi-Fi в Living Room") == "вифи в г+остиная"
    assert prepare.key_words == 2


# --- streaming -----------------------------------------------------------------------------------


def test_split_point_holds_back_numbers_and_phrases() -> None:
    assert split_point("Включаю музыку", 0) == len("Включаю ")
    assert split_point("Сейчас 5 ", 0) == len("Сейчас ")  # the unit may come next
    assert split_point("Сейчас 5 градусов ", 0) == len("Сейчас 5 градусов ")
    assert split_point("в Living Room ", 1) == len("в Living ")
    assert split_point("Привет", 0) == 0


async def started(**config: Any) -> SpeechText:
    node = SpeechText()
    await node.start(RuntimeNodeContext("speech", config, {}, frozenset()))
    return node


def feed(*pieces: str, meta: dict[str, Any] | None = None, end: str = "end") -> InputFeed:
    return InputFeed(items=[TextDelta(p) for p in pieces], meta=meta or {}, end=end)  # type: ignore[arg-type]


async def test_streams_whole_words_and_keeps_the_header() -> None:
    node = await started(stress=True)
    pieces = ("На ули", "це -5 ", "°C, вклю", "чаю Qu", "een.")
    run = await run_node_once(node, {"text": feed(*pieces, meta={"language": "ru"})})
    assert run.error is None, run.error
    out = run.outputs["text"]
    assert out.header is not None and out.header.meta == {"language": "ru"}
    assert "".join(d.text for d in out.items) == "На улице минус пять градусов, включаю Кв+ин."
    assert len(out.items) > 1  # word by word, not all at the end


async def test_speaks_before_the_text_ends() -> None:
    node = await started()
    run = await run_node_once(
        node, {"text": feed("Привет, как ", "дела")}, context={"language": "ru"}
    )
    assert run.error is None, run.error
    # one word is held back: a built-in phrase has two (Home Assistant)
    assert [d.text for d in run.outputs["text"].items] == ["Привет, ", "как дела"]


async def test_other_languages_go_on_unchanged() -> None:
    node = await started()
    run = await run_node_once(
        node, {"text": feed("It is 5 °C", meta={"language": "en"})}, context={"language": "ru"}
    )
    assert run.error is None, run.error
    assert [d.text for d in run.outputs["text"].items] == ["It is 5 °C"]
    # without a language in the header: the turn's
    run = await run_node_once(node, {"text": feed("Сейчас 5 °C")}, context={"language": "ru-RU"})
    assert "".join(d.text for d in run.outputs["text"].items) == "Сейчас пять градусов"


async def test_conforms() -> None:
    report = await check_node(SpeechText)
    assert_conforms(report)
