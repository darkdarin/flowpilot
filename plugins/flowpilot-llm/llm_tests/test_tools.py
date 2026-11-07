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

    async def chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        if self.no_tools and "tools" in body:
            error = {"error": {"message": "model does not support tools"}}
            return web.json_response(error, status=400)
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


async def ask(url: str, store: MemoryStore, text: str, user: str | None, **config: Any) -> Any:
    node = LlmAgent()
    cfg = {"base_url": url, "model": "m", "api_key": {"secret": "k"}} | config
    ctx = RuntimeNodeContext("brain", cfg, {"k": KEY}, frozenset({"k"}), memory=store)
    await node.start(ctx)
    inputs = {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}
    if user:
        who = Principal(user, frozenset(), Assurance.MEDIUM)
        inputs["principal"] = InputFeed(items=[who], meta={}, end="end")
    try:
        run = await run_node_once(node, inputs)
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
    await ask(url, MemoryStore(None), "привет", "masha", tools=False)
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
    await ask(url, MemoryStore(None), "привет", "masha", tools=False)
    known, unknown, off = (r["messages"][0]["content"] for r in fake.requests)
    assert "call the remember tool" in known and "not known who is speaking" not in known
    assert "keep facts as the home's" in unknown
    assert "remember tool" not in off
