"""Home Assistant's quick commands: handled ones are answered, the rest pass on unchanged."""

from __future__ import annotations

import json
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
    # the journal: the request to Home Assistant, then the outcome
    ask, outcome = [e.attrs for e in result.trace.events if e.event == "log"]
    assert ask["kind"] == "quick.ask" and ask["ok"] is True and "try 1" in ask["summary"]
    detail = json.loads(ask["detail"])
    assert detail["request"]["text"] == "включи свет на кухне"
    assert detail["response"]["speech"] == "Включила свет"
    assert outcome["kind"] == "quick" and outcome["ok"] is True


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
    # each try and the decision between them are entries of their own
    kinds = [e.attrs["kind"] for e in result.trace.events if e.event == "log"]
    # (the names Assist knows load once, before the second try)
    assert kinds == ["quick.ask", "quick.names", "quick.retry", "quick.ask", "quick"]
    first, _, retry, second = [e.attrs for e in result.trace.events if e.event == "log"][:4]
    assert first["ok"] is False and second["ok"] is True and "try 2" in second["summary"]
    assert "«включи свет на кухне»" in retry["summary"]


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


async def test_the_sound_says_what_was_done() -> None:
    """The ack carries what was done: turned on, turned off, or something else."""
    here = {**DONE, "targets_in_area": True}
    for intent, kind in (("HassTurnOn", "on"), ("HassTurnOff", "off"), ("HassLightSet", "done")):
        result, _ = await run({**here, "intent": intent})
        (ack,) = result.outputs["ack"].items
        assert ack.data == {"acknowledge": kind}, intent
    result, _ = await run(here)  # an older integration says no intent
    assert result.outputs["ack"].items[0].data == {"acknowledge": "done"}


async def test_a_learned_command_runs_its_actions_without_asking_home_assistant() -> None:
    from flowpilot_sdk import Action
    from flowpilot_server.commands_store import CommandStore

    commands = CommandStore(None)
    movie = commands.add(
        ["режим кино"],
        [
            Action("intent__HassTurnOff", {"area": "Гостиная", "domain": ["light"]}),
            Action("intent__HassTurnOn", {"name": "Телевизор"}),
        ],
        "Приятного просмотра",
        created_by="pavel",
        source="llm.agent",
    )
    done = {"error": False, "data": {"response_type": "action_done"}, "targets_in_area": True}
    host = FakeHa(done)
    node = HaQuickCommands()
    ctx = RuntimeNodeContext("quick", {}, {}, frozenset())
    ctx.commands = commands
    await node.start(ctx)
    result = await run_node_once(
        node,
        {"utterance": InputFeed(items=[Utterance("режим кина, пожалуйста", "ru")], end="end")},
        host=(host := FakeHa(done)),
    )
    assert [(c["name"], c["arguments"]) for c in host.calls] == [
        ("intent__HassTurnOff", {"area": "Гостиная", "domain": ["light"]}),
        ("intent__HassTurnOn", {"name": "Телевизор"}),
    ]
    assert [a.text for a in result.outputs["ack"].items] == ["Приятного просмотра"]
    # one action turned off, the other on: neither sound, so "done"
    assert result.outputs["ack"].items[0].data == {"acknowledge": "done"}
    assert commands.all()[0].uses == 1 and commands.all()[0].id == movie.id


async def test_a_learned_command_that_fails_says_so() -> None:
    from flowpilot_sdk import Action
    from flowpilot_server.commands_store import CommandStore

    commands = CommandStore(None)
    commands.add(
        ["режим кино"],
        [Action("intent__HassTurnOn", {"name": "Нет"})],
        "",
        created_by=None,
        source="editor",
    )
    failed = {"error": True, "data": {"message": "no device named Нет"}}
    node = HaQuickCommands()
    ctx = RuntimeNodeContext("quick", {}, {}, frozenset())
    ctx.commands = commands
    await node.start(ctx)
    run = await run_node_once(
        node,
        {"utterance": InputFeed(items=[Utterance("режим кино", "ru")], end="end")},
        host=FakeHa(failed),
    )
    [answer] = run.outputs["response"].items
    assert answer.text == "Не получилось: no device named Нет"
    assert commands.all()[0].uses == 0
