"""The agent calls tools (remember, forget) and FlowPilot runs them with the speaker's rights."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowpilot_core.conformance import InputFeed, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_llm import LlmAgent
from flowpilot_sdk import Assurance, Principal, Utterance
from flowpilot_server.memory_store import MemoryStore

KEY = "sk-test"


class ToolCallingApi:
    """Answers a first request with the scripted tool calls, the next one with words."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.no_tools = False
        self.tools_need_no_reasoning = False  # like OpenAI's reasoning models in Chat Completions

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        if self.no_tools and "tools" in body:
            error = {"error": {"message": "model does not support tools"}}
            return web.json_response(error, status=400)
        if (
            self.tools_need_no_reasoning
            and "tools" in body
            and body.get("reasoning_effort") != "none"
        ):
            message = (
                "Function tools with reasoning_effort are not supported for gpt-x in "
                "/v1/chat/completions. To use function tools, use /v1/responses or set "
                "reasoning_effort to 'none'."
            )
            return web.json_response({"error": {"message": message}}, status=400)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        resp.force_close()
        await resp.prepare(request)

        async def send(delta: dict[str, Any]) -> None:
            chunk = {"choices": [{"delta": delta, "index": 0}]}
            await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

        last = body["messages"][-1]
        if last["role"] == "user" and self.calls and "tools" in body:
            await send({"content": "Хорошо."})
            for i, call in enumerate(self.calls):
                args = json.dumps(call["arguments"], ensure_ascii=False)
                function = {"name": call["name"], "arguments": args[:5]}
                await send({"tool_calls": [{"index": i, "id": f"c{i}", "function": function}]})
                await send({"tool_calls": [{"index": i, "function": {"arguments": args[5:]}}]})
        else:
            results = [m["content"] for m in body["messages"] if m["role"] == "tool"]
            await send({"content": "Готово: " + " | ".join(results) if results else "Ответ"})
        await resp.write(b"data: [DONE]\n\n")
        return resp


@pytest.fixture
async def api() -> AsyncIterator[tuple[ToolCallingApi, str]]:
    fake = ToolCallingApi()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("/v1"))
    await server.close()


async def ask(
    url: str,
    store: MemoryStore,
    text: str,
    user: str | None,
    *,
    host: Any = None,
    watched: bool = False,
    **config: Any,
) -> Any:
    node = LlmAgent()
    cfg = {"base_url": url, "model": "m", "api_key": {"secret": "k"}} | config
    ctx = RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"}), memory=store)
    await node.start(ctx)
    inputs = {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}
    if user:
        who = Principal(user, frozenset(), Assurance.MEDIUM)
        inputs["principal"] = InputFeed(items=[who], meta={}, end="end")
    try:
        run = await run_node_once(node, inputs, host=host, watched=watched)
    finally:
        await node.stop()
    assert run.error is None, run.error
    return run


async def test_remembers_about_the_speaker(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    memory = MemoryStore(None)
    fake.calls = [
        {"name": "remember", "arguments": {"fact": "Маша не ест мясо.", "about": "speaker"}}
    ]
    run = await ask(url, memory, "запомни, я не ем мясо", "masha")
    [fact] = memory.all()
    assert (fact.text, fact.owner, fact.source) == ("Маша не ест мясо.", "masha", "llm.agent")
    answer = run.outputs["response"].items[0].text
    assert answer == "Хорошо. Готово: remembered about the speaker"  # rounds kept apart
    second = fake.requests[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "remember"
    assert second[-1] == {
        "role": "tool",
        "tool_call_id": "c0",
        "content": "remembered about the speaker",
    }
    tools = [e for e in run.trace.events if e.event == "llm.agent.tool"]
    assert [(e.attrs["tool"], e.attrs["ok"]) for e in tools] == [("remember", True)]


async def test_an_unknown_speaker_keeps_no_personal_facts(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    memory = MemoryStore(None)
    fake.calls = [{"name": "remember", "arguments": {"fact": "Любит чай.", "about": "speaker"}}]
    run = await ask(url, memory, "запомни, я люблю чай", None)
    assert memory.all() == []
    assert "not known who is speaking" in run.outputs["response"].items[0].text


async def test_forgets_only_what_the_speaker_may_see(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    memory = MemoryStore(None)
    memory.add("Кота зовут Барсик.", None, source="editor")
    memory.add("Паша водит Skoda.", "pavel", source="editor")
    fake.calls = [{"name": "forget", "arguments": {"fact": "skoda"}}]
    run = await ask(url, memory, "забудь про шкоду", "masha")
    assert len(memory.all()) == 2  # Pavel's fact is not Masha's to forget
    assert "no such fact" in run.outputs["response"].items[0].text
    fake.calls = [{"name": "forget", "arguments": {"fact": "барсик"}}]
    await ask(url, memory, "забудь про кота", "masha")
    assert [f.text for f in memory.all()] == ["Паша водит Skoda."]


async def test_no_tools_without_memory_or_when_turned_off(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    await ask(url, MemoryStore(None), "привет", "masha", memory="off")
    await ask(url, MemoryStore(None), "привет", "masha", remember="off")
    assert all("tools" not in r for r in fake.requests)
    await ask(url, MemoryStore(None), "привет", "masha")
    assert "tools" in fake.requests[-1]


async def test_a_model_without_function_calling_still_answers(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.no_tools = True
    run = await ask(url, MemoryStore(None), "привет", "masha")
    assert run.outputs["response"].items[0].text == "Ответ"
    assert any(e.event == "llm.agent.no_tools" for e in run.trace.events)
    retry = fake.requests[-1]["messages"]
    assert all("long-term memory" not in m["content"] for m in retry if m["role"] == "system")


async def test_the_agent_is_told_it_can_remember(api: tuple[ToolCallingApi, str]) -> None:
    """Models believe they cannot remember between conversations unless told otherwise."""
    fake, url = api
    await ask(url, MemoryStore(None), "привет", "masha")
    await ask(url, MemoryStore(None), "привет", None)
    await ask(url, MemoryStore(None), "привет", "masha", remember="off")
    known, unknown, off = (r["messages"][0]["content"] for r in fake.requests)
    assert "call the remember tool" in known and "not known who is speaking" not in known
    assert "keep facts as the home's" in unknown
    assert "remember tool" not in off
    assert "on your own" not in known


async def test_on_its_own_the_agent_keeps_what_it_learns(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    memory = MemoryStore(None)
    fake.calls = [
        {"name": "remember", "arguments": {"fact": "Кошку зовут Сиеста.", "about": "home"}}
    ]
    await ask(url, memory, "у нас кошка Сиеста", "pavel", remember="auto")
    system = fake.requests[0]["messages"][0]["content"]
    assert "remember on your own" in system and "health" in system
    [tool] = [t for t in fake.requests[0]["tools"] if t["function"]["name"] == "remember"]
    assert "lasting fact about the home" in tool["function"]["description"]
    [fact] = memory.all()
    assert (fact.text, fact.owner) == ("Кошку зовут Сиеста.", None)
    run = await ask(url, memory, "кошка Сиеста", "pavel", remember="auto")
    assert len(memory.all()) == 1  # the same fact is not kept twice
    assert "already remembered" in run.outputs["response"].items[0].text


async def test_tools_turn_reasoning_off_when_the_provider_asks(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.tools_need_no_reasoning = True
    memory = MemoryStore(None)
    fake.calls = [
        {"name": "remember", "arguments": {"fact": "Кошку зовут Сиеста.", "about": "home"}}
    ]
    node = LlmAgent()
    cfg = {"base_url": url, "model": "m", "api_key": {"secret": "k"}}
    ctx = RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"}), memory=memory)
    await node.start(ctx)
    try:
        for text in ("запомни: кошку зовут Сиеста", "привет"):
            inputs = {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}
            run = await run_node_once(node, inputs)
            assert run.error is None, run.error
            if text.startswith("запомни"):
                events = [e.event for e in run.trace.events]
                assert "llm.agent.no_reasoning" in events and "llm.agent.no_tools" not in events
    finally:
        await node.stop()
    assert [f.text for f in memory.all()] == ["Кошку зовут Сиеста."]
    efforts = [r.get("reasoning_effort") for r in fake.requests]
    assert efforts[0] is None and set(efforts[1:]) == {"none"}  # refused once, then off for good


async def test_reasoning_is_sent_only_when_set(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    await ask(url, MemoryStore(None), "привет", "masha")
    await ask(url, MemoryStore(None), "привет", "masha", reasoning="high")
    assert "reasoning_effort" not in fake.requests[0]
    assert fake.requests[1]["reasoning_effort"] == "high"
    fake.tools_need_no_reasoning = True
    fake.requests.clear()
    run = await ask(url, MemoryStore(None), "привет", "masha", reasoning="none")
    assert len(fake.requests) == 1 and "tools" in fake.requests[0]  # no refusal, no retry
    assert all(e.event != "llm.agent.no_reasoning" for e in run.trace.events)


class PeopleHost:
    """Home Assistant's side: lists the home's people (``ha.people.list``)."""

    def __init__(self, people: list[dict[str, Any]] | None) -> None:
        self.people = people
        self.calls = 0

    def offers(self, op: str) -> bool:
        return op == "ha.people.list" and self.people is not None

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return {"people": self.people}

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


HOME: list[dict[str, Any]] = [
    {"name": "Павел", "user_id": "pavel", "entity_id": "person.pavel", "state": "home"},
    {"name": "Анастасия", "user_id": "nastya", "entity_id": "person.nastya", "state": "Работа"},
    {"name": "Григорий", "user_id": None, "entity_id": "person.grisha", "state": "not_home"},
    {"name": "Гость", "user_id": "guest", "entity_id": None, "state": None},
]


async def test_the_agent_knows_who_it_talks_to(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    run = await ask(url, MemoryStore(None), "кто я?", "pavel", host=PeopleHost(HOME))
    system = fake.requests[0]["messages"][0]["content"]
    assert "- Павел (at home)" in system and "- Анастасия (at Работа)" in system
    assert "- Григорий (away)" in system and "- Гость\n" in system
    assert "You are talking to Павел." in system
    [summary] = [e for e in run.trace.events if e.event == "llm.agent"]
    assert summary.attrs["people"] == 4
    await ask(url, MemoryStore(None), "кто я?", None, host=PeopleHost(HOME))
    assert "not known which of them" in fake.requests[1]["messages"][0]["content"]


async def test_no_people_without_home_assistant_or_when_off(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    await ask(url, MemoryStore(None), "привет", "pavel")  # no client offers the list
    off = PeopleHost(HOME)
    await ask(url, MemoryStore(None), "привет", "pavel", host=off, people=False)
    await ask(url, MemoryStore(None), "привет", "pavel", host=PeopleHost(None))  # old integration
    assert off.calls == 0
    assert all("People of the home" not in r["messages"][0]["content"] for r in fake.requests)


class HomeHost:
    """Home Assistant's Assist LLM API (``ha.llm.tools``, ``ha.llm.call``)."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def offers(self, op: str) -> bool:
        return op in {"ha.llm.tools", "ha.llm.call"}

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        self.calls.append((op, dict(args)))
        if op == "ha.llm.tools":
            schema = {"type": "object", "properties": {"name": {"type": "string"}}}
            tool = {"name": "intent__HassTurnOn", "description": "Turn on", "parameters": schema}
            return {"prompt": "Use the tools to control the home.", "tools": [tool]}
        if self.fail:
            return {"error": True, "data": {"message": "no device named Kitchen"}}
        return {"error": False, "data": {"success": True, "name": args["arguments"]["name"]}}

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


async def test_the_agent_controls_the_home_as_the_speaker(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    host = HomeHost()
    fake.calls = [{"name": "intent__HassTurnOn", "arguments": {"name": "Kitchen"}}]
    run = await ask(url, MemoryStore(None), "включи свет на кухне", "pavel", host=host)
    first = fake.requests[0]
    assert "Use the tools to control the home." in first["messages"][0]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == {
        "remember",
        "forget",
        "intent__HassTurnOn",
    }
    [(_, listed), (_, called)] = host.calls
    assert listed["user_id"] == "pavel"
    assert called == {
        "user_id": "pavel",
        "device_id": None,
        "language": "ru",
        "name": "intent__HassTurnOn",
        "arguments": {"name": "Kitchen"},
    }
    assert '"success": true' in fake.requests[1]["messages"][-1]["content"]
    tools = [e for e in run.trace.events if e.event == "llm.agent.tool"]
    assert [(e.attrs["tool"], e.attrs["ok"]) for e in tools] == [("intent__HassTurnOn", True)]


async def test_home_errors_reach_the_model(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    fake.calls = [{"name": "intent__HassTurnOn", "arguments": {"name": "Kitchen"}}]
    run = await ask(url, MemoryStore(None), "включи свет", None, host=HomeHost(fail=True))
    assert "no device named Kitchen" in fake.requests[1]["messages"][-1]["content"]
    tools = [e for e in run.trace.events if e.event == "llm.agent.tool"]
    assert [e.attrs["ok"] for e in tools] == [False]


async def test_no_home_tools_when_turned_off(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    host = HomeHost()
    await ask(url, MemoryStore(None), "привет", "pavel", host=host, home=False, remember="off")
    assert host.calls == [] and "tools" not in fake.requests[0]


async def test_the_journal_shows_what_the_agent_did_only_in_live_debug(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    memory = MemoryStore(None)
    memory.add("Кошку зовут Сиеста.", None, source="editor")
    fake.calls = [
        {"name": "remember", "arguments": {"fact": "Паша любит рок.", "about": "speaker"}}
    ]
    quiet = await ask(url, memory, "запомни, я люблю рок", "pavel")
    assert not [e for e in quiet.trace.events if e.event == "log"]  # nobody watches: no content

    run = await ask(
        url, memory, "запомни, я люблю рок", "pavel", host=PeopleHost(HOME), watched=True
    )
    log = [e.attrs for e in run.trace.events if e.event == "log"]
    kinds = [e["kind"] for e in log]
    assert kinds == [
        "memory",
        "context",
        "llm.request",
        "llm.response",
        "tool",
        "llm.request",
        "llm.response",
    ]
    memory_entry, context = log[0], log[1]
    assert "home: Кошку зовут Сиеста." in memory_entry["detail"]
    assert memory_entry["summary"].endswith("of Павел; dialogue: 0 earlier exchanges")
    assert "You are talking to Павел." in context["detail"]  # the whole system prompt
    assert log[2]["detail"] == "запомни, я люблю рок"
    tool = log[4]
    assert tool["tool"] == "remember" and tool["ok"] is True and "ms" in tool
    assert '"fact": "Паша любит рок."' in tool["detail"] and "already remembered" in tool["detail"]
    assert "calls remember" in log[3]["summary"] and log[6]["detail"].startswith("Готово")


async def test_the_agent_sees_exchanges_other_nodes_answered(
    api: tuple[ToolCallingApi, str],
) -> None:
    """A quick command answered without the agent is part of the dialogue it continues."""
    from flowpilot_core.runtime.deployment import SHARED_NAMESPACE
    from flowpilot_core.runtime.state import MemorySessionStore
    from flowpilot_sdk import dialogue

    fake, url = api
    sessions = MemorySessionStore()
    shared = sessions.view("s1", SHARED_NAMESPACE)
    await dialogue.add(shared, "выключи свет на кухне", "Выключила свет", by="quick")
    node = LlmAgent()
    cfg = {
        "base_url": url,
        "model": "m",
        "api_key": {"secret": "k"},
        "remember": "off",
        "home": False,
    }
    await node.start(RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"})))
    try:
        inputs = {
            "utterance": InputFeed(items=[Utterance("включи обратно", "ru")], meta={}, end="end")
        }
        run = await run_node_once(node, inputs, sessions=sessions, session_id="s1")
    finally:
        await node.stop()
    assert run.error is None, run.error
    said = [m["content"] for m in fake.requests[0]["messages"] if m["role"] != "system"]
    assert said == ["выключи свет на кухне", "Выключила свет", "включи обратно"]
    kept = await dialogue.exchanges(shared)
    assert [(e["user"], e["by"]) for e in kept] == [
        ("выключи свет на кухне", "quick"),
        ("включи обратно", "brain"),
    ]


def test_a_long_pause_starts_the_dialogue_afresh() -> None:
    import time

    from flowpilot_sdk import dialogue

    old = [{"user": "a", "assistant": "b", "by": "x", "at": time.time() - 7200}]
    assert dialogue.recent(old, turns=10, quiet_s=3600) == []
    assert dialogue.recent(old, turns=10, quiet_s=10800) == old
    assert dialogue.recent(old, turns=0, quiet_s=10800) == []


async def test_the_agent_teaches_a_quick_command_after_doing_it(
    api: tuple[ToolCallingApi, str],
) -> None:
    """Turn 1: the agent turns the light on. Turn 2: "yes, remember it": it saves the calls."""
    from flowpilot_core.runtime.state import MemorySessionStore
    from flowpilot_server.commands_store import CommandStore

    fake, url = api
    commands = CommandStore(None)
    sessions = MemorySessionStore()
    node = LlmAgent()
    cfg = {"base_url": url, "model": "m", "api_key": {"secret": "k"}, "remember": "off"}
    ctx = RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"}))
    ctx.commands = commands
    await node.start(ctx)
    pavel = InputFeed(items=[Principal("pavel", frozenset(), Assurance.MEDIUM)], end="end")

    async def say(text: str) -> Any:
        inputs = {
            "utterance": InputFeed(items=[Utterance(text, "ru")], end="end"),
            "principal": pavel,
        }
        run = await run_node_once(node, inputs, host=HomeHost(), sessions=sessions, session_id="s")
        assert run.error is None, run.error
        return run

    try:
        fake.calls = [{"name": "intent__HassTurnOn", "arguments": {"name": "Люстра"}}]
        await say("вруби люстру")
        first = fake.requests[0]
        assert "save_quick_command" in {t["function"]["name"] for t in first["tools"]}
        assert "teach the home quick commands" in first["messages"][0]["content"]
        fake.requests.clear()
        save = {
            "phrases": ["вруби люстру"],
            "actions": [{"tool": "intent__HassTurnOn", "arguments": {"name": "Люстра"}}],
            "reply": "Люстра горит",
        }
        fake.calls = [{"name": "save_quick_command", "arguments": save}]
        await say("да, запомни")
        # the calls of the previous answer were shown to the model
        assert '"intent__HassTurnOn"' in fake.requests[0]["messages"][0]["content"]
    finally:
        await node.stop()
    [command] = commands.all()
    assert command.phrases == ("вруби люстру",) and command.reply == "Люстра горит"
    assert [(a.tool, dict(a.arguments)) for a in command.actions] == [
        ("intent__HassTurnOn", {"name": "Люстра"})
    ]
    assert command.created_by == "pavel" and command.source == "llm.agent"


async def test_only_home_tools_and_known_people_teach() -> None:
    from flowpilot_llm.tools import Toolbox, command_tools
    from flowpilot_server.commands_store import CommandStore

    commands = CommandStore(None)
    known = Toolbox(command_tools(commands, "pavel", {"intent__HassTurnOn"}))
    nobody = Toolbox(command_tools(commands, None, {"intent__HassTurnOn"}))
    action = {"tool": "intent__HassTurnOn", "arguments": {"name": "Люстра"}}
    ok = json.dumps({"phrases": ["люстра"], "actions": [action]})
    _, done = await nobody.call("save_quick_command", ok)
    assert not done and not commands.all()
    stranger = json.dumps({"phrases": ["x"], "actions": [{"tool": "remember", "arguments": {}}]})
    _, done = await known.call("save_quick_command", stranger)
    assert not done
    result, done = await known.call("save_quick_command", ok)
    assert done and "люстра" in result and len(commands.all()) == 1
    result, done = await known.call("forget_quick_command", json.dumps({"phrase": "люстра"}))
    assert done and commands.all() == []
