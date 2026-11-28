"""The way back from an MCP server's OAuth sign-in to the FlowPilot runtime (ADR-025).

The runtime's editor opens in Home Assistant through Ingress, whose session cookie is
``SameSite=Strict``: a provider (or the project's https return page) sending the browser back to
an Ingress address would arrive without it and be refused. So the return page sends the browser
here instead, to Home Assistant's own address, and this view hands the provider's answer to the
runtime it belongs to. No login is needed: the sign-in's ``state``, which only the runtime that
started it knows, is the proof, and without the runtime's PKCE verifier a code is useless.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import web
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

RUNTIME_CALLBACK = "/api/mcp/oauth/callback"
KNOWN_HEADER = "X-FlowPilot-Login"  # the runtime says whether the sign-in was its own
MAX_QUERY = 8192
TIMEOUT_S = 10.0

NOT_FOUND = (
    "<!doctype html><meta charset=utf-8><title>FlowPilot</title>"
    "<body style='font-family:system-ui;padding:2em'><h2>Не получилось</h2>"
    "<p>Этот вход устарел или уже завершён. Начните вход заново в редакторе FlowPilot.</p>"
)


class OAuthCallbackView(HomeAssistantView):
    url = "/api/flowpilot/oauth/callback"
    name = "api:flowpilot:oauth_callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        query = request.query_string
        if len(query) > MAX_QUERY or "state=" not in query:
            return web.Response(status=400, text="bad request")
        session = async_get_clientsession(hass)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            base = str(entry.data[CONF_URL]).rstrip("/")
            try:
                async with session.get(
                    f"{base}{RUNTIME_CALLBACK}?{query}",
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                ) as answer:
                    page = await answer.text()
                    known = answer.headers.get(KNOWN_HEADER) == "known"
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.warning("FlowPilot at %s did not take the sign-in: %s", base, err)
                continue
            if known:
                return web.Response(text=page, content_type="text/html")
        return web.Response(text=NOT_FOUND, content_type="text/html")


def register(hass: Any) -> None:
    """Add the view once per Home Assistant."""
    key = f"{DOMAIN}_oauth_view"
    if not hass.data.get(key):
        hass.http.register_view(OAuthCallbackView())
        hass.data[key] = True
