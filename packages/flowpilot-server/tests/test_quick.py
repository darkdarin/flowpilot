"""Home Assistant's quick commands: handled ones are answered, the rest pass on unchanged."""

from __future__ import annotations

from typing import Any

from flowpilot_core.conformance import InputFeed, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_core.runtime.deployment import SHARED_NAMESPACE
from flowpilot_core.runtime.state import MemorySessionStore
from flowpilot_sdk import HostUnavailable, Utterance, dialogue
from flowpilot_server.quick import HaQuickCommands


class FakeHa:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def offers(self, op: str) -> bool:
        return self.answer is not None

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(args))
        if self.answer is None:
            raise HostUnavailable(op)
        return self.answer

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


async def run(answer: dict[str, Any] | None, text: str = "включи свет на кухне") -> Any:
    node = HaQuickCommands()
    await node.start(RuntimeNodeContext("quick", {}, {}, frozenset()))
    sessions = MemorySessionStore()
    inputs = {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}
    result = await run_node_once(
        node, inputs, host=FakeHa(answer), sessions=sessions, session_id="s", watched=True
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
