"""The agent looks things up on the web (``web_search``) with the provider of its settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flowpilot_llm import search
from flowpilot_llm.search import WebSearch, result_text
from flowpilot_server.memory_store import MemoryStore

from .test_tools import ToolCallingApi, ask


class FakeSearch:
    """Tavily, Brave and SearXNG on one test server."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, str], Any]] = []
        self.status = 200

    def _seen(self, request: web.Request, body: Any = None) -> None:
        self.requests.append((request.path, dict(request.query), dict(request.headers), body))

    async def tavily(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._seen(request, body)
        if self.status != 200:
            return web.json_response({"detail": {"error": "Unauthorized"}}, status=self.status)
        return web.json_response(
            {
                "answer": "1 BTC ≈ 91 000 $.",
                "results": [
                    {
                        "title": "Курс биткоина",
                        "url": "https://a.example",
                        "content": "BTC 91 000 $",
                    }
                ],
            }
        )

    async def brave(self, request: web.Request) -> web.Response:
        self._seen(request)
        return web.json_response(
            {
                "web": {
                    "results": [
                        {
                            "title": "Матч <strong>Реал</strong>",
                            "url": "https://b.example",
                            "description": "Реал выиграл &amp; вышел в финал",
                            "extra_snippets": ["Счёт 2:1"],
                        }
                    ]
                }
            }
        )

    async def searxng(self, request: web.Request) -> web.Response:
        self._seen(request)
        if self.status != 200:
            return web.Response(status=self.status, text="Forbidden")
        results = [
            {"title": f"Результат {i}", "url": f"https://s{i}.example", "content": "текст"}
            for i in range(8)
        ]
        return web.json_response({"answers": ["Прадо открыт до 20:00"], "results": results})


@pytest.fixture
async def provider(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[FakeSearch, str]]:
    fake = FakeSearch()
    app = web.Application()
    app.router.add_post("/tavily", fake.tavily)
    app.router.add_get("/brave", fake.brave)
    app.router.add_get("/searx/search", fake.searxng)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(search, "TAVILY_URL", str(server.make_url("/tavily")))
    monkeypatch.setattr(search, "BRAVE_URL", str(server.make_url("/brave")))
    yield fake, str(server.make_url("/searx"))
    await server.close()


@pytest.fixture
async def api() -> AsyncIterator[tuple[ToolCallingApi, str]]:
    fake = ToolCallingApi()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.chat)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("/v1"))
    await server.close()


async def test_tavily_gives_its_answer_and_extracts(provider: tuple[FakeSearch, str]) -> None:
    fake, _ = provider
    async with aiohttp.ClientSession() as http:
        answer, found = await WebSearch("tavily", key="tvly-1", results=3).search(
            http, "курс биткоина", "ru"
        )
    assert answer == "1 BTC ≈ 91 000 $."
    assert [(f.title, f.url, f.text) for f in found] == [
        ("Курс биткоина", "https://a.example", "BTC 91 000 $")
    ]
    _, _, headers, body = fake.requests[0]
    assert headers["Authorization"] == "Bearer tvly-1"
    assert body["query"] == "курс биткоина" and body["max_results"] == 3


async def test_brave_extracts_are_plain_text(provider: tuple[FakeSearch, str]) -> None:
    fake, _ = provider
    async with aiohttp.ClientSession() as http:
        answer, found = await WebSearch("brave", key="b-1").search(http, "реал", "ru")
    assert answer == ""
    assert (found[0].title, found[0].text) == ("Матч Реал", "Реал выиграл & вышел в финал Счёт 2:1")
    _, query, headers, _ = fake.requests[0]
    assert query["q"] == "реал" and headers["X-Subscription-Token"] == "b-1"


async def test_searxng_searches_in_the_turns_language(provider: tuple[FakeSearch, str]) -> None:
    fake, url = provider
    async with aiohttp.ClientSession() as http:
        answer, found = await WebSearch("searxng", url=url + "/", results=2).search(
            http, "прадо часы работы", "ru-RU"
        )
    assert answer == "Прадо открыт до 20:00"
    assert [f.url for f in found] == ["https://s0.example", "https://s1.example"]
    _, query, _, _ = fake.requests[0]
    assert query == {"q": "прадо часы работы", "format": "json", "language": "ru"}


async def test_searxng_without_json_says_how_to_fix_it(provider: tuple[FakeSearch, str]) -> None:
    fake, url = provider
    fake.status = 403
    async with aiohttp.ClientSession() as http:
        with pytest.raises(search.SearchError, match="search.formats json"):
            await WebSearch("searxng", url=url).search(http, "q", "ru")


def test_a_provider_needs_its_key_or_address() -> None:
    with pytest.raises(ValueError, match="API key"):
        WebSearch("tavily")
    with pytest.raises(ValueError, match="address"):
        WebSearch("searxng")
    with pytest.raises(ValueError, match="unknown"):
        WebSearch("google", key="k")


def test_the_result_fits_the_prompt() -> None:
    found = [search.Found(f"t{i}", f"https://{i}.example", "слово " * 500) for i in range(10)]
    text = result_text("q", "", found)
    assert len(text) <= search.RESULT_CHARS + len(search.ANSWER_HINT) + 1
    assert text.endswith(search.ANSWER_HINT)
    assert "Nothing was found." in result_text("q", "", [])


async def test_the_agent_searches_and_answers_from_the_results(
    api: tuple[ToolCallingApi, str], provider: tuple[FakeSearch, str]
) -> None:
    fake, url = api
    _, searx = provider
    fake.say_first = False
    fake.calls = [{"name": "web_search", "arguments": {"query": "часы работы Прадо"}}]
    run = await ask(
        url,
        MemoryStore(None),
        "до скольки открыт Прадо?",
        None,
        web_search="searxng",
        web_search_url=searx,
        memory="off",
    )
    first = fake.requests[0]
    assert "web_search" in [t["function"]["name"] for t in first["tools"]]
    assert "never say you have no internet access" in first["messages"][0]["content"]
    result = fake.requests[1]["messages"][-1]
    assert result["role"] == "tool"
    assert "Short answer: Прадо открыт до 20:00" in result["content"]
    assert "never read out site addresses" in result["content"]
    tools = [e for e in run.trace.events if e.event == "llm.agent.tool"]
    assert [(e.attrs["tool"], e.attrs["ok"]) for e in tools] == [("web_search", True)]


async def test_a_failed_search_is_told_to_the_model(
    api: tuple[ToolCallingApi, str], provider: tuple[FakeSearch, str]
) -> None:
    fake, url = api
    searcher, searx = provider
    searcher.status = 403
    fake.calls = [{"name": "web_search", "arguments": {"query": "q"}}]
    run = await ask(
        url,
        MemoryStore(None),
        "что нового?",
        None,
        web_search="searxng",
        web_search_url=searx,
        memory="off",
    )
    assert "the web search failed" in fake.requests[1]["messages"][-1]["content"]
    assert run.outputs["response"].items


async def test_no_search_unless_set(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    await ask(url, MemoryStore(None), "привет", None, memory="off")
    assert "tools" not in fake.requests[0]
    assert all("web_search" not in str(m["content"]) for m in fake.requests[0]["messages"])


async def test_a_provider_without_its_key_is_left_out(api: tuple[ToolCallingApi, str]) -> None:
    fake, url = api
    run = await ask(url, MemoryStore(None), "привет", None, web_search="tavily", memory="off")
    assert "tools" not in fake.requests[0]
    [event] = [e for e in run.trace.events if e.event == "llm.agent.no_web_search"]
    assert "API key" in event.attrs["reason"]
