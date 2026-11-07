"""The Text converter: any text type in, all of them out."""

from __future__ import annotations

from typing import Any

import pytest

from flowpilot_core.builtin import TextConvert
from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_sdk import Response, TextDelta, Transcript, Utterance


async def converter(config: dict[str, Any] | None = None) -> TextConvert:
    node = TextConvert()
    await node.start(RuntimeNodeContext("conv", config or {}, {}, frozenset()))
    return node


def stream(*items: Any) -> InputFeed:
    return InputFeed(items=list(items), meta={}, end="end")


def value(item: Any) -> InputFeed:
    return InputFeed(items=[item], meta={}, end="end")


def texts(run: Any) -> dict[str, Any]:
    out = run.outputs
    return {
        "transcript": [(t.text, t.final) for t in out["transcript"].items],
        "utterance": [u.text for u in out["utterance"].items],
        "delta": [d.text for d in out["delta"].items],
        "response": [r.text for r in out["response"].items],
    }


async def test_a_text_stream_becomes_everything_else() -> None:
    run = await run_node_once(
        await converter(), {"text": stream(TextDelta("Сейчас "), TextDelta("три часа"))}
    )
    assert run.error is None, run.error
    assert texts(run) == {
        "transcript": [("Сейчас три часа", True)],
        "utterance": ["Сейчас три часа"],
        "delta": ["Сейчас ", "три часа"],  # the stream passes through piece by piece
        "response": ["Сейчас три часа"],
    }


async def test_recognized_speech_keeps_its_partials() -> None:
    items = (Transcript("сколько", final=False), Transcript("сколько времени", True, "ru"))
    run = await run_node_once(await converter(), {"text": stream(*items)})
    assert run.error is None, run.error
    got = texts(run)
    assert got["transcript"] == [("сколько", False), ("сколько времени", True)]
    assert got["delta"] == ["сколько времени"] and got["response"] == ["сколько времени"]
    assert run.outputs["utterance"].items == [Utterance("сколько времени", "ru")]


async def test_an_answer_speaks_its_speech_text() -> None:
    answer = Response(text="Сейчас 22 °C", speech_text="Сейчас двадцать два градуса", language="ru")
    run = await run_node_once(await converter(), {"text": value(answer)})
    assert run.error is None, run.error
    assert texts(run)["delta"] == ["Сейчас двадцать два градуса"]
    assert texts(run)["utterance"] == ["Сейчас 22 °C"]
    assert run.outputs["response"].items == [answer]  # passes through unchanged
    plain = await run_node_once(await converter({"spoken": False}), {"text": value(answer)})
    assert texts(plain)["delta"] == ["Сейчас 22 °C"]


async def test_an_utterance_becomes_an_answer() -> None:
    run = await run_node_once(await converter(), {"text": value(Utterance("привет", "ru"))})
    assert run.error is None, run.error
    assert run.outputs["response"].items == [Response(text="привет", language="ru")]
    assert texts(run)["delta"] == ["привет"]


async def test_empty_text_gives_only_the_transcript() -> None:
    run = await run_node_once(await converter(), {"text": value(Utterance("  "))})
    assert run.error is None, run.error
    assert texts(run) == {
        "transcript": [("", True)],
        "utterance": [],
        "delta": [],
        "response": [],
    }


async def test_absent_input_absent_outputs() -> None:
    run = await run_node_once(await converter(), {"text": InputFeed(items=[], end="absent")})
    assert run.error is None
    assert all(not o.items for o in run.outputs.values())


@pytest.mark.parametrize(
    "sample",
    [
        {"text": stream(TextDelta("hi"))},
        {"text": value(Response(text="hi"))},
    ],
)
async def test_conforms(sample: dict[str, InputFeed]) -> None:
    assert_conforms(await check_node(TextConvert, sample=sample))
