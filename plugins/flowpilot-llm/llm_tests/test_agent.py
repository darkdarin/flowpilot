"""The LLM agent against a fake OpenAI-compatible API, alone and through a real runtime."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowpilot_core import NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_core.conformance import InputFeed, assert_conforms, check_node, run_node_once
from flowpilot_core.runtime import RuntimeNodeContext
from flowpilot_llm import LlmAgent, LlmError
from flowpilot_sdk import Utterance
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.nodes import ClientSink, ClientSource
from flowpilot_server.protocol import TURN_PATH
from flowpilot_server.secret_store import SecretStore
from flowpilot_server.server import FlowPilotServer

KEY = "sk-test"


class FakeOpenAI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.fail = False

    async def models(self, request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "gpt-small"}, {"id": "gpt-big"}]})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        self.auth.append(request.headers.get("Authorization", ""))
        body = await request.json()
        self.requests.append(body)
        if self.fail:
            error = {"error": {"message": "Incorrect API key provided", "type": "invalid"}}
            return web.json_response(error, status=401)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        # the fake server shares the test's event loop: a kept-alive connection would leave its
        # handler task waiting, which the conformance kit counts against the node
        resp.force_close()
        await resp.prepare(request)
        answer = f"Ответ на «{body['messages'][-1]['content']}»"
        for piece in (answer[:6], answer[6:]):
            chunk = {"choices": [{"delta": {"content": piece}, "index": 0}]}
            await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeOpenAI, str]]:
    api = FakeOpenAI()
    app = web.Application()
    app.router.add_get("/v1/models", api.models)
    app.router.add_post("/v1/chat/completions", api.chat)
    server = TestServer(app)
    await server.start_server()
    yield api, str(server.make_url("/v1"))
    await server.close()


async def agent(url: str, **config: Any) -> LlmAgent:
    node = LlmAgent()
    cfg = {"base_url": url, "model": "gpt-small", "api_key": {"secret": "openai"}} | config
    await node.start(RuntimeNodeContext("brain", cfg, {"openai": KEY}, frozenset({"openai"})))
    return node


def said(text: str) -> dict[str, InputFeed]:
    return {"utterance": InputFeed(items=[Utterance(text, "ru")], meta={}, end="end")}


async def test_streams_the_answer(fake: tuple[FakeOpenAI, str]) -> None:
    api, url = fake
    node = await agent(url, system_prompt="Сегодня {date}. Отвечай на языке {language}.")
    try:
        run = await run_node_once(node, said("который час"))
    finally:
        await node.stop()
    assert run.error is None, run.error
    pieces = [d.text for d in run.outputs["delta"].items]
    assert len(pieces) == 2 and "".join(pieces) == "Ответ на «который час»"
    assert run.outputs["response"].items[0].text == "Ответ на «который час»"
    request = api.requests[0]
    assert request["stream"] is True and request["model"] == "gpt-small"
    system = request["messages"][0]
    assert system["role"] == "system" and "{date}" not in system["content"]
    assert "языке ru" in system["content"]
    assert api.auth == [f"Bearer {KEY}"]
    (event,) = [e for e in run.trace.events if e.event == "llm.agent"]
    assert event.attrs["first_token_ms"] is not None and event.attrs["chars"] > 0


async def test_reports_provider_errors(fake: tuple[FakeOpenAI, str]) -> None:
    api, url = fake
    api.fail = True
    node = await agent(url)
    try:
        run = await run_node_once(node, said("привет"))
    finally:
        await node.stop()
    assert isinstance(run.error, LlmError) and "Incorrect API key" in str(run.error)


async def test_lists_models_with_the_resolved_key(fake: tuple[FakeOpenAI, str]) -> None:
    _, url = fake
    models = await LlmAgent.config_options("model", {"base_url": url, "api_key": KEY})
    assert [m.id for m in models] == ["gpt-big", "gpt-small"]


async def test_remembers_the_dialogue(fake: tuple[FakeOpenAI, str], tmp_path: Path) -> None:
    """Two turns of one conversation through the runtime: the second carries the first."""
    api, url = fake
    registry = register_builtin(NodeRegistry())
    for cls in (ClientSource, ClientSink, LlmAgent):
        registry.register(cls)
    store = GraphStore(tmp_path)
    config = {"base_url": url, "model": "gpt-small", "api_key": {"secret": "openai"}}
    store.save(
        "main",
        {
            "schema_version": 1,
            "id": "main",
            "nodes": [
                {"id": "in", "type": "client.source"},
                {"id": "brain", "type": "llm.agent", "config": config | {"history_turns": 1}},
                {"id": "out", "type": "client.sink"},
            ],
            "edges": [
                {"from": "in.utterance", "to": "brain.utterance"},
                {"from": "brain.delta", "to": "out.delta"},
                {"from": "brain.response", "to": "out.response"},
            ],
        },
    )
    secrets = SecretStore(None, {"openai": KEY})
    manager = GraphManager(registry, store=store, secrets=secrets)
    assert await manager.load_all() == {}
    server = TestServer(FlowPilotServer(token="t", default_graph="main", manager=manager).app())
    await server.start_server()
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer t"}) as http:
            for text in ("первый", "второй", "третий"):
                body = {"text": text, "conversation_id": "dialog-1"}
                resp = await http.post(server.make_url(TURN_PATH), json=body)
                assert (await resp.json())["response"]["text"] == f"Ответ на «{text}»"
            other = await http.post(server.make_url(TURN_PATH), json={"text": "чужой"})
            assert other.status == 200
    finally:
        await server.close()
        await manager.stop()
    contents = [[m["content"] for m in r["messages"]] for r in api.requests]
    assert contents[0] == ["первый"]
    assert contents[1] == ["первый", "Ответ на «первый»", "второй"]
    assert contents[2] == ["второй", "Ответ на «второй»", "третий"]  # only the last exchange
    assert contents[3] == ["чужой"]  # another conversation starts clean


async def test_conforms(fake: tuple[FakeOpenAI, str]) -> None:
    _, url = fake
    config = {"base_url": url, "model": "gpt-small", "api_key": {"secret": "openai"}}
    report = await check_node(
        LlmAgent, config=config, secrets={"openai": KEY}, sample=said("привет")
    )
    assert_conforms(report)
