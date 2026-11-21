"""``web_search``: the agent looks up fresh facts on the web (exchange rates, news, opening hours).

The model calls it only when it needs what it does not know and the home does not have, so plain
commands stay as fast as before. The search goes to one provider of the node's settings:

- Tavily (``api.tavily.com``): built for language models, gives ready extracts; needs a key;
- Brave Search API: needs a key (``X-Subscription-Token``);
- SearXNG: a self-hosted metasearch without a key; its JSON output must be on
  (``search.formats: [html, json]`` in its ``settings.yml``).

The result is a short text for the model: a few titles, addresses and extracts, cut to fit the
prompt.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from .tools import Tool

PROVIDERS = ("tavily", "brave", "searxng")
TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_TIMEOUT_S = 10.0
DEFAULT_RESULTS = 5
EXTRACT_CHARS = 600  # one result's extract
RESULT_CHARS = 4000  # everything the model gets back

DESCRIPTION = (
    "Search the web for current facts you do not know and the home does not have: news, "
    "exchange rates and prices, weather in other places, sports results, opening hours, events, "
    "anything that may have changed after your training. Not for the home's devices and not for "
    "what you can answer yourself. Write the query as a short search phrase, in the language "
    "that suits the topic best."
)
ANSWER_HINT = (
    "Answer from these results in a sentence or two, in the user's language; say the numbers, "
    "never read out site addresses. If the results do not answer the question, say so."
)

_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class Found:
    title: str
    url: str
    text: str


class SearchError(Exception):
    pass


def _clean(text: Any) -> str:
    """Plain text of a provider's extract: no HTML tags or entities, one line."""
    return " ".join(html.unescape(_TAGS.sub("", str(text or ""))).split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def result_text(query: str, answer: str, found: list[Found]) -> str:
    """What the model gets back: the date, the provider's own short answer and the results."""
    lines = [f"Web search for «{query}» on {datetime.now().astimezone():%Y-%m-%d}:"]
    if answer:
        lines.append(f"Short answer: {_cut(_clean(answer), EXTRACT_CHARS)}")
    for i, item in enumerate(found, 1):
        lines.append(f"{i}. {item.title} ({item.url})\n{_cut(item.text, EXTRACT_CHARS)}")
    if not answer and not found:
        lines.append("Nothing was found.")
    lines.append(ANSWER_HINT)
    text = "\n".join(lines)
    return text if len(text) <= RESULT_CHARS else _cut(text, RESULT_CHARS) + "\n" + ANSWER_HINT


class WebSearch:
    """One provider of the node's settings, on the node's HTTP session."""

    def __init__(
        self,
        provider: str,
        *,
        key: str | None = None,
        url: str | None = None,
        results: int = DEFAULT_RESULTS,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown web search provider {provider!r}")
        if provider in ("tavily", "brave") and not key:
            raise ValueError(f"web search with {provider} needs its API key")
        if provider == "searxng" and not url:
            raise ValueError("web search with SearXNG needs its address")
        self.provider = provider
        self._key = key
        self._url = (url or "").rstrip("/")
        self._results = max(1, min(int(results), 10))

    async def search(
        self, http: aiohttp.ClientSession, query: str, language: str | None
    ) -> tuple[str, list[Found]]:
        """The provider's short answer (Tavily, SearXNG, when they have one) and the results."""
        timeout = aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_S)
        base = (language or "").split("-")[0].casefold()
        if self.provider == "tavily":
            body = {
                "query": query,
                "max_results": self._results,
                "search_depth": "basic",
                "include_answer": True,
            }
            headers = {"Authorization": f"Bearer {self._key}"}
            data = await self._json(
                http.post(TAVILY_URL, json=body, headers=headers, timeout=timeout)
            )
            found = [
                Found(_clean(r.get("title")), str(r.get("url") or ""), _clean(r.get("content")))
                for r in data.get("results") or []
                if isinstance(r, Mapping)
            ]
            return str(data.get("answer") or ""), found
        if self.provider == "brave":
            params = {"q": query, "count": str(self._results)}
            headers = {"X-Subscription-Token": str(self._key), "Accept": "application/json"}
            data = await self._json(
                http.get(BRAVE_URL, params=params, headers=headers, timeout=timeout)
            )
            web: Mapping[str, Any] = data.get("web") or {}
            if not isinstance(web, Mapping):
                web = {}
            found = [
                Found(
                    _clean(r.get("title")),
                    str(r.get("url") or ""),
                    " ".join(
                        _clean(t) for t in [r.get("description"), *(r.get("extra_snippets") or [])]
                    ).strip(),
                )
                for r in web.get("results") or []
                if isinstance(r, Mapping)
            ]
            return "", found[: self._results]
        params = {"q": query, "format": "json"}
        if base:
            params["language"] = base
        data = await self._json(http.get(f"{self._url}/search", params=params, timeout=timeout))
        answers = [
            _clean(a.get("answer") if isinstance(a, Mapping) else a)
            for a in data.get("answers") or []
        ]
        found = [
            Found(_clean(r.get("title")), str(r.get("url") or ""), _clean(r.get("content")))
            for r in data.get("results") or []
            if isinstance(r, Mapping)
        ]
        return " ".join(a for a in answers if a), found[: self._results]

    async def _json(self, request: Any) -> Mapping[str, Any]:
        try:
            async with request as resp:
                if resp.status != 200:
                    text = _cut(_clean(await resp.text()), 200)
                    if self.provider == "searxng" and resp.status == 403:
                        text = "SearXNG refuses JSON: turn on search.formats json in settings.yml"
                    raise SearchError(f"{self.provider} answered {resp.status}: {text}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SearchError(
                f"{self.provider} is unreachable: {err or type(err).__name__}"
            ) from err
        if not isinstance(data, Mapping):
            raise SearchError(f"{self.provider} gave no results")
        return data

    def tool(self, http: aiohttp.ClientSession, language: str | None) -> Tool:
        """``web_search`` for one turn."""

        async def run(args: Mapping[str, Any]) -> str:
            query = " ".join(str(args.get("query") or "").split())
            if not query:
                raise ValueError("give the query")
            try:
                answer, found = await self.search(http, query, language)
            except SearchError as err:
                raise ValueError(
                    f"the web search failed: {err}; say you could not look it up"
                ) from err
            return result_text(query, answer, found)

        return Tool(
            "web_search",
            DESCRIPTION,
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for."}},
                "required": ["query"],
            },
            run,
        )
