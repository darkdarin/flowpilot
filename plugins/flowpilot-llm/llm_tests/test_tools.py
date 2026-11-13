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
        self.say_first = True  # a few words before the tool calls

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
            if self.say_first:
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
        "confirm_with_sound",  # Client output's ack is connected
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


async def test_changing_a_quick_command_never_makes_a_copy() -> None:
    from flowpilot_llm.tools import Toolbox, command_tools
    from flowpilot_server.commands_store import CommandStore

    commands = CommandStore(None)
    tools = Toolbox(command_tools(commands, "pavel", {"intent__HassTurnOn", "intent__HassTurnOff"}))
    on = {"tool": "intent__HassTurnOn", "arguments": {"name": "ТВ"}}
    off = {"tool": "intent__HassTurnOff", "arguments": {"area": "Гостиная"}}
    await tools.call("save_quick_command", json.dumps({"phrases": ["режим кино"], "actions": [on]}))
    # saved again with one of its phrases (and more): the same command changes
    again = {"phrases": ["Режим кино!", "кино"], "actions": [off, on], "reply": "Приятного"}
    result, ok = await tools.call("save_quick_command", json.dumps(again))
    assert ok and result.startswith("updated")
    [command] = commands.all()
    assert command.phrases == ("Режим кино!", "кино") and len(command.actions) == 2

    change = {"phrase": "кино", "add_phrases": ["фильм"], "remove_phrases": ["кино"], "reply": "Ок"}
    result, ok = await tools.call("update_quick_command", json.dumps(change))
    assert ok, result
    [command] = commands.all()
    assert command.phrases == ("Режим кино!", "фильм") and command.reply == "Ок"
    assert len(command.actions) == 2  # kept: not asked to change them
    _, ok = await tools.call("update_quick_command", json.dumps({"phrase": "нет такой"}))
    assert not ok


async def test_the_agent_uses_a_skill_and_home_assistant_runs_it(
    api: tuple[ToolCallingApi, str],
) -> None:
    from flowpilot_server.skills_store import SkillStore

    fake, url = api
    skills = SkillStore(None)
    skills.add(
        {
            "name": "tv_open_app",
            "description": "Open an app on the living room TV",
            "fields": [{"name": "app", "type": "choice", "options": ["youtube", "kinopoisk"]}],
            "actions": [
                {
                    "action": "androidtv.adb_command",
                    "target": {"entity_id": "media_player.tv"},
                    "data": {"command": "monkey -p {app} 1"},
                }
            ],
        }
    )

    class SkillHost(HomeHost):
        def offers(self, op: str) -> bool:
            return op in {"ha.llm.tools", "ha.llm.call", "ha.service.call"}

        async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
            if op == "ha.service.call":
                self.calls.append((op, dict(args)))
                return {"error": False, "data": {"done": True}}
            return await super().call(turn_id, op, args, **kwargs)

    host = SkillHost()
    node = LlmAgent()
    cfg = {"base_url": url, "model": "m", "api_key": {"secret": "k"}, "remember": "off"}
    ctx = RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"}))
    ctx.skills = skills
    await node.start(ctx)
    fake.calls = [{"name": "skill__tv_open_app", "arguments": {"app": "youtube"}}]
    try:
        said = InputFeed(items=[Utterance("включи ютуб на телевизоре", "ru")], end="end")
        run = await run_node_once(node, {"utterance": said}, host=host)
    finally:
        await node.stop()
    assert run.error is None, run.error
    offered = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert "skill__tv_open_app" in offered
    [(_, call)] = [c for c in host.calls if c[0] == "ha.service.call"]
    assert call["action"] == "androidtv.adb_command"
    assert call["data"] == {"command": "monkey -p youtube 1"}
    assert call["target"] == {"entity_id": "media_player.tv"}


class ActingHost:
    """Home Assistant's intents as the Assist LLM API runs them: their answer and where the
    devices are."""

    def __init__(self, *, here: bool = True, fail: str | None = None) -> None:
        self.here = here
        self.fail = fail  # the device that is not found
        self.asked: list[Any] = []  # what the agent asked for the tools with

    def offers(self, op: str) -> bool:
        return op in {"ha.llm.tools", "ha.llm.call"}

    async def call(self, turn_id: str, op: str, args: Any, **kwargs: Any) -> Any:
        if op == "ha.llm.tools":
            schema = {"type": "object", "properties": {"name": {"type": "string"}}}
            tools = [
                {"name": name, "description": name, "parameters": schema}
                for name in ("HassTurnOn", "HassTurnOff", "GetLiveContext", "flowpilot__play_music")
            ]
            self.asked.append(args)
            answer: dict[str, Any] = {"prompt": "Use the tools.", "tools": tools}
            if args.get("live_context"):
                answer["live_context"] = "- names: Свет\n  domain: light\n  state: 'on'"
            return answer
        name, device = args["name"], args["arguments"].get("name")
        if self.fail is not None and device == self.fail:
            return {"error": True, "data": {"message": f"no device named {device}"}}
        if name == "GetLiveContext":
            return {"error": False, "data": {"success": True, "result": "light: on"}}
        if name == "flowpilot__play_music":
            playing = {"playing": "Сплин", "type": "artist", "status": "starting"}
            return {"error": False, "data": playing, "say": "Включаю Сплин"}
        said = {"HassTurnOn": "Включила", "HassTurnOff": "Выключила"}[name]
        data = {"response_type": "action_done", "speech": {"plain": {"speech": f"{said} {device}"}}}
        return {"error": False, "data": data, "targets_in_area": self.here}

    def stream(self, turn_id: str, op: str, args: Any) -> Any:
        raise NotImplementedError


def turn_off(*devices: str) -> list[dict[str, Any]]:
    return [{"name": "HassTurnOff", "arguments": {"name": d}} for d in devices] + [
        {"name": "confirm_with_sound", "arguments": {}}
    ]


async def test_a_done_command_is_confirmed_with_a_sound_at_once(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.say_first = False
    fake.calls = turn_off("свет", "торшер")
    run = await ask(url, MemoryStore(None), "выключи свет", "pavel", host=ActingHost())
    assert len(fake.requests) == 1  # no second request for words
    assert "confirm_with_sound" in {t["function"]["name"] for t in fake.requests[0]["tools"]}
    (ack,) = run.outputs["ack"].items
    assert ack.data == {"acknowledge": "off"} and ack.text == "Выключила свет Выключила торшер"
    assert run.outputs["delta"].items == []
    assert [r.text for r in run.outputs["response"].items] == [ack.text]
    (done,) = [e for e in run.trace.events if e.event == "llm.agent"]
    assert done.attrs["acknowledge"] == "off"


@pytest.mark.parametrize(
    ("host", "calls", "config", "why"),
    [
        (ActingHost(fail="торшер"), turn_off("свет", "торшер"), {}, "an action failed"),
        (ActingHost(here=False), turn_off("свет"), {}, "not every device is in the room"),
        (
            ActingHost(),
            [{"name": "GetLiveContext", "arguments": {}}, *turn_off()],
            {},
            "nothing was done",
        ),
    ],
)
async def test_otherwise_the_agent_answers_in_words(
    api: tuple[ToolCallingApi, str], host: ActingHost, calls: Any, config: Any, why: str
) -> None:
    fake, url = api
    fake.say_first = False
    fake.calls = calls
    run = await ask(url, MemoryStore(None), "выключи свет", "pavel", host=host, **config)
    assert run.outputs["ack"].items == [] and run.outputs["delta"].items
    told = [m["content"] for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert any(why in t for t in told), told


async def test_a_sound_anywhere_and_never(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    fake.say_first = False
    fake.calls = turn_off("свет")
    away = ActingHost(here=False)
    run = await ask(url, MemoryStore(None), "выключи", "pavel", host=away, acknowledge="model")
    assert run.outputs["ack"].items and len(fake.requests) == 1
    fake.requests.clear()
    run = await ask(url, MemoryStore(None), "выключи", "pavel", host=away, acknowledge="never")
    assert "confirm_with_sound" not in {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert not run.outputs["ack"].items


async def test_words_already_said_are_not_followed_by_a_sound(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.calls = turn_off("свет")  # «Хорошо.» comes first
    run = await ask(url, MemoryStore(None), "выключи", "pavel", host=ActingHost())
    assert not run.outputs["ack"].items and len(fake.requests) == 2


async def test_music_on_its_way_is_told_without_asking_the_model_again(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.say_first = False
    fake.calls = [{"name": "flowpilot__play_music", "arguments": {"artist": "Сплин"}}]
    run = await ask(url, MemoryStore(None), "включи сплин", "pavel", host=ActingHost())
    assert len(fake.requests) == 1  # the words came with the tool's answer
    assert "".join(d.text for d in run.outputs["delta"].items) == "Включаю Сплин."
    assert [r.text for r in run.outputs["response"].items] == ["Включаю Сплин."]
    assert not run.outputs["ack"].items
    (done,) = [e for e in run.trace.events if e.event == "llm.agent"]
    assert done.attrs["told"] is True


async def test_a_reply_with_more_than_music_goes_on_to_the_model(
    api: tuple[ToolCallingApi, str],
) -> None:
    fake, url = api
    fake.say_first = False
    fake.calls = [
        {"name": "flowpilot__play_music", "arguments": {"artist": "Сплин"}},
        {"name": "GetLiveContext", "arguments": {}},
    ]
    await ask(url, MemoryStore(None), "включи сплин", "pavel", host=ActingHost())
    assert len(fake.requests) == 2


async def test_the_models_own_words_are_not_repeated(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    fake.calls = [{"name": "flowpilot__play_music", "arguments": {"artist": "Сплин"}}]
    run = await ask(url, MemoryStore(None), "включи сплин", "pavel", host=ActingHost())
    assert len(fake.requests) == 1
    assert [r.text for r in run.outputs["response"].items] == ["Хорошо."]


async def test_the_home_state_is_in_the_prompt(api: tuple[ToolCallingApi, str]) -> None:
    """The model sees which lights are on and acts at once, without asking for the state."""
    fake, url = api
    host = ActingHost()
    await ask(url, MemoryStore(None), "выключи свет", "pavel", host=host)
    assert host.asked[0]["live_context"] is True
    system = fake.requests[0]["messages"][0]["content"]
    assert system.rstrip().endswith("state: 'on'")  # last: the rest of the prompt is cached
    assert "The state of the home as this request began" in system


async def test_the_home_state_can_be_left_out(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    host = ActingHost()
    await ask(url, MemoryStore(None), "выключи свет", "pavel", host=host, home_state=False)
    assert host.asked[0]["live_context"] is False
    assert "state: 'on'" not in fake.requests[0]["messages"][0]["content"]
