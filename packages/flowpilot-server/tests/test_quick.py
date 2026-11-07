"""Home Assistant's quick commands: handled ones are answered, the rest pass on unchanged."""

from __future__ import annotations

from typing import Any

from flowpilot_core.conformance import InputFeed, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_core.runtime.deployment import SHARED_NAMESPACE
from flowpilot_core.runtime.state import MemorySessionStore
from flowpilot_sdk import HostUnavailable, Utterance, dialogue
from flowpilot_server.quick import HaQuickCommands, cleaned


class FakeHa:
    """Answers each request with the next of ``answers`` (the last one repeats)."""

    def __init__(self, *answers: dict[str, Any] | None, names: list[str] | None = None) -> None:
        self.answers = list(answers)
        self.names = names
        self.calls: list[dict[str, Any]] = []

    def offers(self, op: str) -> bool:
        if op == "ha.names.list":
            return self.names is not None
        return self.answers[0] is not None

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        if op == "ha.names.list":
            return {"names": self.names}
        self.calls.append(dict(args))
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if answer is None:
            raise HostUnavailable(op)
        return answer

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


async def run(
    answer: dict[str, Any] | None,
    text: str = "включи свет на кухне",
    *more: dict[str, Any],
    host: FakeHa | None = None,
    config: dict[str, Any] | None = None,
) -> Any:
    node = HaQuickCommands()
    await node.start(RuntimeNodeContext("quick", config or {}, {}, frozenset()))
    sessions = MemorySessionStore()
    inputs = {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}
    result = await run_node_once(
        node,
        inputs,
        host=host or FakeHa(answer, *more),
        sessions=sessions,
        session_id="s",
        watched=True,
    )
    assert result.error is None, result.error
    shared = sessions.view("s", SHARED_NAMESPACE)
    return result, await dialogue.exchanges(shared)


async def test_a_command_home_assistant_did_is_answered() -> None:
    done = {"response_type": "action_done", "speech": "Включила свет", "error_code": None}
    result, kept = await run(done)
    assert [r.text for r in result.outputs["response"].items] == ["Включила свет"]
    assert [d.text for d in result.outputs["delta"].items] == ["Включила свет"]
    assert result.outputs["unhandled"].items == []
    # the agent after this node will know what was done
    assert [(e["user"], e["assistant"], e["by"]) for e in kept] == [
        ("включи свет на кухне", "Включила свет", "quick")
    ]
    [log] = [e.attrs for e in result.trace.events if e.event == "log"]
    assert log["kind"] == "quick" and log["ok"] is True


async def test_what_it_did_not_understand_passes_on_unchanged() -> None:
    missed = {"response_type": "error", "speech": "Не поняла", "error_code": "no_intent_match"}
    result, kept = await run(missed, "расскажи анекдот")
    assert result.outputs["unhandled"].items == [Utterance("расскажи анекдот", "ru")]
    assert result.outputs["response"].items == [] and result.outputs["delta"].items == []
    assert kept == []  # the agent answers it and adds it itself
    [event] = [e for e in result.trace.events if e.event == "ha.quick"]
    assert event.attrs["handled"] is False and event.attrs["code"] == "no_intent_match"


async def test_without_home_assistant_everything_passes_on() -> None:
    result, _ = await run(None)
    assert result.outputs["unhandled"].items == [Utterance("включи свет на кухне", "ru")]


DONE = {"response_type": "action_done", "speech": "Включила свет", "error_code": None}
MISSED = {"response_type": "error", "speech": "Не поняла", "error_code": "no_intent_match"}


def test_extra_words_are_left_out() -> None:
    assert cleaned("Алиса, включи-ка, пожалуйста, свет на кухне!", "ru", ["алиса"]) == (
        "включи свет на кухне"
    )
    assert cleaned("Слушай, а можешь выключить телевизор?", "ru", []) == "выключить телевизор"
    assert cleaned("Please, could you turn on the kitchen light?", "en", []) == (
        "turn on the kitchen light"
    )
    assert cleaned("Включи ёлку", "ru-RU", []) == "включи елку"


async def test_a_second_try_without_extra_words() -> None:
    host = FakeHa(MISSED, DONE)
    result, _ = await run(None, "Слушай, включи-ка свет на кухне, пожалуйста", host=host)
    assert [c["text"] for c in host.calls] == [
        "Слушай, включи-ка свет на кухне, пожалуйста",
        "включи свет на кухне",
    ]
    assert [r.text for r in result.outputs["response"].items] == ["Включила свет"]
    [event] = [e for e in result.trace.events if e.event == "ha.quick"]
    assert event.attrs["retried"] is True and event.attrs["handled"] is True


async def test_no_second_try_when_understood_or_turned_off() -> None:
    no_target = {"response_type": "error", "speech": "Нет такого", "error_code": "no_valid_targets"}
    host = FakeHa(no_target, DONE)
    await run(None, "пожалуйста, включи свет в гараже", host=host)
    assert len(host.calls) == 1  # it understood: the words are not the problem
    off = FakeHa(MISSED, DONE)
    await run(None, "пожалуйста, включи свет", host=off, config={"retry_cleaned": False})
    assert len(off.calls) == 1
    clean = FakeHa(MISSED, DONE)
    await run(None, "расскажи анекдот", host=clean)
    assert len(clean.calls) == 1  # nothing to leave out: no point asking again


async def test_names_are_put_back_in_their_own_form() -> None:
    """Home Assistant knows "Гостиная", not "гостиной": the second try uses its name."""
    no_target = {"response_type": "error", "speech": "Нет такого", "error_code": "no_valid_targets"}
    host = FakeHa(no_target, DONE, names=["Гостиная", "Настольная лампа"])
    result, _ = await run(None, "Включи, пожалуйста, свет в гостиной", host=host)
    assert [c["text"] for c in host.calls][1] == "включи свет в Гостиная"
    assert [r.text for r in result.outputs["response"].items] == ["Включила свет"]
    lamp = FakeHa(MISSED, DONE, names=["Гостиная", "Настольная лампа"])
    await run(None, "выключи настольную лампу", host=lamp)
    assert lamp.calls[1]["text"] == "выключи Настольная лампа"
    # no such target and no name to fix: asking again would not help
    garage = FakeHa(no_target, DONE, names=["Гостиная"])
    await run(None, "включи свет в гараже", host=garage)
    assert len(garage.calls) == 1


async def test_a_sound_instead_of_words_in_the_speakers_room() -> None:
    here = {**DONE, "targets_in_area": True}
    result, _ = await run(here)
    assert [a.text for a in result.outputs["ack"].items] == ["Включила свет"]
    assert result.outputs["delta"].items == []  # nothing to speak
    assert [r.text for r in result.outputs["response"].items] == ["Включила свет"]  # for chats
    elsewhere = {**DONE, "targets_in_area": False}
    away, _ = await run(elsewhere)
    assert away.outputs["ack"].items == [] and [d.text for d in away.outputs["delta"].items] == [
        "Включила свет"
    ]
    always, _ = await run(elsewhere, config={"acknowledge": "always"})
    assert always.outputs["ack"].items and not always.outputs["delta"].items
    never, _ = await run(here, config={"acknowledge": "never"})
    assert not never.outputs["ack"].items and never.outputs["delta"].items
