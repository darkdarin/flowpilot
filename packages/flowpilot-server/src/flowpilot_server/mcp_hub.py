"""Connections to the runtime's MCP servers (``flowpilot_sdk.McpServers``, ADR-025).

One background task per enabled server keeps an MCP session open (Streamable HTTP or SSE, the
official ``mcp`` SDK), lists the tools and lists them again when the server says they changed;
a lost connection comes back with a growing pause. A turn never waits for a connection: a server
that is not ready simply offers no tools.

Sign-in with OAuth is the SDK's ``OAuthClientProvider`` (discovery, client metadata document or
dynamic registration, PKCE, refresh) with the tokens kept in ``McpStore``. It needs a person only
to sign in: the editor asks ``login`` for the provider's address, opens it through the project's
https return page, and the provider's answer comes back to ``callback``. Without tokens, or when
a refresh is refused, the server waits in ``needs_auth`` instead of retrying.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ListenNotSupportedError
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from flowpilot_sdk import McpResult, McpServer, McpTool, McpUnavailable
from flowpilot_sdk.mcp import ServerState

from .mcp_store import McpServerConfig, McpStore

_LOGGER = logging.getLogger(__name__)

# the project's https page the provider returns to; it forwards to the runtime (docs/oauth/)
REDIRECT_URL = "https://darkdarin.github.io/flowpilot/oauth/"
CLIENT_METADATA_URL = "https://darkdarin.github.io/flowpilot/oauth/client.json"
CLIENT_NAME = "FlowPilot"

RELIST_EVERY_S = 120.0
BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
LOGIN_URL_TIMEOUT_S = 30.0  # discovery and registration before the provider's address
LOGIN_TIMEOUT_S = 600.0  # a person signing in
MAX_RESULT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    redirect_url: str = REDIRECT_URL
    client_metadata_url: str | None = CLIENT_METADATA_URL


class SignInNeeded(Exception):
    """The server needs a person to sign in."""


class _Login:
    """A sign-in a person started: the provider's address, then its answer."""

    def __init__(self) -> None:
        loop = asyncio.get_running_loop()
        self.url: asyncio.Future[str] = loop.create_future()
        self.answer: asyncio.Future[AuthorizationCodeResult] = loop.create_future()
        self.state: str | None = None


class _Storage:
    """``TokenStorage`` of the SDK over ``McpStore``."""

    def __init__(self, store: McpStore, server: McpServerConfig, redirect_url: str) -> None:
        self._store, self._server, self._redirect = store, server, redirect_url

    async def get_tokens(self) -> OAuthToken | None:
        data = self._store.secrets(self._server.id).oauth_tokens
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._store.save_oauth(self._server.id, tokens=tokens.model_dump(mode="json"))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        secrets = self._store.secrets(self._server.id)
        if self._server.client_id:  # registered by hand at the provider
            return OAuthClientInformationFull.model_validate(
                {
                    "client_id": self._server.client_id,
                    "client_secret": secrets.client_secret or None,
                    "redirect_uris": [self._redirect],
                    "token_endpoint_auth_method": (
                        "client_secret_post" if secrets.client_secret else "none"
                    ),
                }
            )
        data = secrets.oauth_client
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if not self._server.client_id:
            self._store.save_oauth(self._server.id, client=client_info.model_dump(mode="json"))


def _tool_of(server_id: str, tool: Any) -> McpTool:
    notes = tool.annotations
    read_only = bool(notes and notes.read_only_hint)
    destructive = (
        bool(notes.destructive_hint)
        if notes and notes.destructive_hint is not None
        else not read_only
    )
    title = tool.title or (notes.title if notes else None) or ""
    return McpTool(
        server=server_id,
        name=tool.name,
        description=" ".join((tool.description or title or "").split()),
        parameters=dict(tool.input_schema or {"type": "object", "properties": {}}),
        title=title,
        read_only=read_only,
        destructive=destructive,
    )


def result_text(result: Any) -> McpResult:
    """A tool's result as text for a model: its text blocks, else its structured content."""
    parts: list[str] = []
    for block in result.content or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind == "resource" and getattr(block.resource, "text", None):
            parts.append(block.resource.text)
        else:
            parts.append(f"[{kind or 'content'} omitted]")
    if result.structured_content is not None and not any(p.strip("[] ") for p in parts):
        parts = [json.dumps(result.structured_content, ensure_ascii=False)]
    text = "\n".join(parts).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " …[cut]"
    return McpResult(text=text, is_error=bool(result.is_error))


class _Connection:
    def __init__(self, hub: McpHub, server: McpServerConfig) -> None:
        self.hub = hub
        self.server = server
        self.state: ServerState = "connecting" if server.enabled else "disabled"
        self.error: str | None = None
        self.tools: list[McpTool] = []
        self.client: Client | None = None
        self.login: _Login | None = None
        self._relist = asyncio.Event()
        self._refused: int | None = None  # the last 4xx/5xx HTTP status (the SDK hides it)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if not self.server.enabled:
            self._set("disabled")
            return
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.server.name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.client = None
        if self.login is not None and not self.login.answer.done():
            self.login.answer.cancel()

    def _set(self, state: ServerState, error: str | None = None) -> None:
        self.state, self.error = state, error
        if state != "ready":
            self.client, self.tools = None, []
        self.hub.changed()

    async def _run(self) -> None:
        attempt = 0
        while True:
            secrets = self.hub.store.secrets(self.server.id)
            if self.server.auth == "oauth" and not secrets.oauth_tokens and self.login is None:
                self._set("needs_auth", "sign in to use this server")
                return
            self._set("connecting")
            try:
                await self._session()
                attempt = 0  # it was up; a drop reconnects at once
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure: report and retry
                if (needed := _find_sign_in(err)) is not None and self.login is None:
                    self._set("needs_auth", str(needed) or "sign in again to use this server")
                    return
                message = _describe(err)
                if self._refused in (401, 403) and self.server.auth != "oauth":
                    message = f"the server refused access (HTTP {self._refused}): check the token"
                elif self._refused is not None and "HTTP" not in message:
                    message = f"{message} (HTTP {self._refused})"
                _LOGGER.warning("MCP server %s: %s", self.server.name, message)
                if self.login is not None:  # a sign-in that failed: show it, do not loop
                    if not self.login.url.done():
                        self.login.url.set_exception(SignInNeeded(message))
                    self.login = None
                    self._set("needs_auth", f"sign-in failed: {message}")
                    return
                self._set("error", message)
                await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1

    async def _session(self) -> None:
        async with contextlib.AsyncExitStack() as stack:
            auth = self._auth()
            headers = self._headers()
            if self.server.transport == "sse":
                transport = sse_client(self.server.url, headers=headers, auth=auth)
            else:
                http = await stack.enter_async_context(
                    create_mcp_http_client(headers=headers, auth=auth)
                )
                http.event_hooks["response"].append(self._seen)
                transport = streamable_http_client(self.server.url, http_client=http)
            client = await stack.enter_async_context(
                Client(transport, message_handler=self._on_message, cache=None)
            )
            self.login = None  # signed in (or did not need to)
            tools = await self._list(client)
            self.client, self.tools = client, tools
            self._set_ready()
            listening = asyncio.create_task(
                self._listen(client), name=f"mcp-listen:{self.server.name}"
            )
            stack.push_async_callback(_cancelled, listening)
            while True:  # list again on a change, and now and then: it also checks the link
                self._relist.clear()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(RELIST_EVERY_S):
                        await self._relist.wait()
                self.tools = await self._list(client)
                self.hub.changed()

    async def _seen(self, response: httpx2.Response) -> None:
        self._refused = response.status_code if response.status_code >= 400 else None

    async def _listen(self, client: Client) -> None:
        """Changes of the tool list: a subscription with servers of the 2026-07-28 protocol;
        older servers send notifications (``_on_message``) instead."""
        try:
            async with client.listen(tools_list_changed=True) as events:
                async for _ in events:
                    self._relist.set()
        except ListenNotSupportedError:
            return
        except Exception as err:  # noqa: BLE001 - the periodic listing still catches up
            _LOGGER.debug("MCP server %s: no change events: %s", self.server.name, err)

    def _set_ready(self) -> None:
        self.state, self.error = "ready", None
        self.hub.changed()
        _LOGGER.info("MCP server %s: %d tools", self.server.name, len(self.tools))

    async def _list(self, client: Client) -> list[McpTool]:
        tools: list[McpTool] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools += [_tool_of(self.server.id, t) for t in page.tools]
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def _on_message(self, message: Any) -> None:
        inner = getattr(message, "root", message)
        if getattr(inner, "method", "") == "notifications/tools/list_changed":
            self._relist.set()

    def _headers(self) -> dict[str, str] | None:
        token = self.hub.store.secrets(self.server.id).token
        if self.server.auth == "token" and token:
            return {"Authorization": f"Bearer {token}"}
        return None

    def _auth(self) -> httpx2.Auth | None:
        if self.server.auth != "oauth":
            return None
        oauth = self.hub.oauth
        secret = self.hub.store.secrets(self.server.id).client_secret
        metadata = OAuthClientMetadata.model_validate(
            {
                "client_name": CLIENT_NAME,
                "redirect_uris": [oauth.redirect_url],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post" if secret else "none",
                "scope": self.server.scope or None,
            }
        )
        return OAuthClientProvider(
            server_url=self.server.url,
            client_metadata=metadata,
            storage=_Storage(self.hub.store, self.server, oauth.redirect_url),
            redirect_handler=self._redirect,
            callback_handler=self._callback,
            client_metadata_url=None if self.server.client_id else oauth.client_metadata_url,
        )

    async def _redirect(self, url: str) -> None:
        login = self.login
        if login is None:  # in the background: no one to sign in
            raise SignInNeeded("sign in again to use this server")
        states = parse_qs(urlsplit(url).query).get("state")
        login.state = states[0] if states else None
        if not login.url.done():
            login.url.set_result(url)

    async def _callback(self) -> AuthorizationCodeResult:
        login = self.login
        if login is None:
            raise SignInNeeded("sign in again to use this server")
        async with asyncio.timeout(LOGIN_TIMEOUT_S):
            return await login.answer


async def _cancelled(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _find_sign_in(err: BaseException) -> SignInNeeded | None:
    """The ``SignInNeeded`` behind an error, also inside the SDK's exception groups."""
    if isinstance(err, SignInNeeded):
        return err
    if isinstance(err, BaseExceptionGroup):
        for inner in err.exceptions:
            if (found := _find_sign_in(inner)) is not None:
                return found
    cause = err.__cause__ or err.__context__
    return _find_sign_in(cause) if cause is not None and cause is not err else None


def _describe(err: BaseException) -> str:
    """A short reason for people, also for an exception group from the SDK's task groups."""
    while isinstance(err, BaseExceptionGroup) and err.exceptions:
        err = err.exceptions[0]
    if isinstance(err, httpx2.HTTPStatusError):
        return f"HTTP {err.response.status_code} from {err.request.url.host}"
    if isinstance(err, httpx2.ConnectError):
        return f"cannot connect: {err}"
    text = str(err) or type(err).__name__
    return text if len(text) < 300 else text[:300] + "…"


class McpHub:
    """The runtime's MCP servers: their connections and tools, for nodes and the editor."""

    def __init__(self, store: McpStore, oauth: OAuthSettings | None = None) -> None:
        self.store = store
        self.oauth = oauth or OAuthSettings()
        self._connections: dict[str, _Connection] = {}
        self._listeners: list[Callable[[], None]] = []
        self._started = False

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        self._started = True
        for server in self.store.all():
            self._connect(server)

    async def stop(self) -> None:
        self._started = False
        connections, self._connections = list(self._connections.values()), {}
        await asyncio.gather(*(c.stop() for c in connections))

    def on_change(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def changed(self) -> None:
        for listener in self._listeners:
            listener()

    def _connect(self, server: McpServerConfig) -> _Connection:
        connection = _Connection(self, server)
        self._connections[server.id] = connection
        if self._started:
            connection.start()
        return connection

    async def _restart(self, server: McpServerConfig, login: _Login | None = None) -> _Connection:
        old = self._connections.pop(server.id, None)
        if old is not None:
            await old.stop()
        connection = _Connection(self, server)
        connection.login = login
        self._connections[server.id] = connection
        if self._started:
            connection.start()
        return connection

    # --- flowpilot_sdk.McpServers ---------------------------------------------------------------

    def servers(self) -> list[McpServer]:
        result = []
        for server in self.store.all():
            connection = self._connections.get(server.id)
            state: ServerState = connection.state if connection else "connecting"
            result.append(
                McpServer(
                    id=server.id,
                    name=server.name,
                    title=server.title,
                    state=state,
                    error=connection.error if connection else None,
                )
            )
        return result

    def tools(self, servers: Iterable[str] | None = None) -> list[McpTool]:
        wanted = set(servers) if servers is not None else None
        tools: list[McpTool] = []
        for server in self.store.all():
            if wanted is not None and server.id not in wanted and server.name not in wanted:
                continue
            connection = self._connections.get(server.id)
            if connection is None or connection.state != "ready":
                continue
            off = set(server.disabled_tools)
            tools += [t for t in connection.tools if t.name not in off]
        return tools

    async def call(
        self, server: str, tool: str, arguments: Mapping[str, Any], *, timeout_s: float
    ) -> McpResult:
        connection = self._connections.get(server)
        client = connection.client if connection else None
        if connection is None or client is None or connection.state != "ready":
            raise McpUnavailable(f"the MCP server {server!r} is not connected")
        if tool in connection.server.disabled_tools:
            raise McpUnavailable(f"the tool {tool!r} is turned off")
        try:
            async with asyncio.timeout(timeout_s):
                result = await client.call_tool(tool, dict(arguments))
        except TimeoutError as err:
            raise McpUnavailable(f"{connection.server.title} did not answer in time") from err
        except Exception as err:  # noqa: BLE001 - the server failed the call
            raise McpUnavailable(_describe(err)) from err
        return result_text(result)

    # --- the editor ---------------------------------------------------------------------------

    def public(self) -> list[dict[str, Any]]:
        result = []
        for server in self.store.all():
            connection = self._connections.get(server.id)
            known = connection.tools if connection else []
            off = set(server.disabled_tools)
            result.append(
                {
                    **self.store.public(server),
                    "state": connection.state if connection else "connecting",
                    "error": connection.error if connection else None,
                    "tools": [
                        {
                            "name": t.name,
                            "title": t.title,
                            "description": t.description,
                            "read_only": t.read_only,
                            "destructive": t.destructive,
                            "enabled": t.name not in off,
                        }
                        for t in known
                    ],
                }
            )
        return result

    async def add(self, data: Any) -> McpServerConfig:
        server = self.store.add(data)
        self._connect(server)
        self.changed()
        return server

    async def update(self, server_id: str, data: Any) -> McpServerConfig:
        old = self.store.get(server_id)
        server, reconnect = self.store.update(server_id, data)
        if reconnect or old is None or old.enabled != server.enabled:
            await self._restart(server)
        elif connection := self._connections.get(server_id):
            connection.server = server
        self.changed()
        return server

    async def set_tools(self, server_id: str, disabled: list[str]) -> McpServerConfig:
        server = self.store.set_tools(server_id, disabled)
        if connection := self._connections.get(server_id):
            connection.server = server
        self.changed()
        return server

    async def remove(self, server_id: str) -> bool:
        connection = self._connections.pop(server_id, None)
        if connection is not None:
            await connection.stop()
        removed = self.store.remove(server_id)
        self.changed()
        return removed

    async def reconnect(self, server_id: str) -> None:
        server = self.store.get(server_id)
        if server is None:
            raise KeyError(server_id)
        await self._restart(server)

    async def sign_out(self, server_id: str) -> None:
        server = self.store.get(server_id)
        if server is None:
            raise KeyError(server_id)
        self.store.sign_out(server_id)
        await self._restart(server)

    async def login(self, server_id: str) -> str:
        """Start signing in: the provider's address to open (``SignInNeeded`` when the server
        cannot be signed in to)."""
        server = self.store.get(server_id)
        if server is None:
            raise KeyError(server_id)
        if server.auth != "oauth":
            raise SignInNeeded("this server does not sign in with OAuth")
        if not server.enabled:
            raise SignInNeeded("the server is turned off")
        self.store.sign_out(server_id)  # signing in again replaces the tokens
        login = _Login()
        connection = await self._restart(server, login)
        loop = asyncio.get_running_loop()
        until = loop.time() + LOGIN_URL_TIMEOUT_S
        while not login.url.done():
            if connection.state == "ready":
                raise SignInNeeded("the server let FlowPilot in without signing in")
            if connection.login is None and connection.state in ("needs_auth", "error"):
                raise SignInNeeded(connection.error or "the server's sign-in did not start")
            if loop.time() > until:
                raise SignInNeeded("the server's sign-in did not start in time")
            await asyncio.wait([login.url], timeout=0.1)
        return login.url.result()  # raises the reason when discovery failed

    def callback(self, state: str, code: str | None, error: str | None) -> bool:
        """The provider's answer for a sign-in; whether a sign-in waited for it."""
        for connection in self._connections.values():
            login = connection.login
            if login is None or login.state is None or login.state != state:
                continue
            if login.answer.done():
                return False
            if code:
                login.answer.set_result(AuthorizationCodeResult(code=code, state=state))
            else:
                login.answer.set_exception(SignInNeeded(f"the provider refused: {error}"))
            return True
        return False


def callback_page(ok: bool, message: str) -> str:
    """The small page the sign-in window shows, telling the editor and closing itself."""
    payload = json.dumps({"type": "flowpilot-mcp-login", "ok": ok, "message": message})
    title = "Готово" if ok else "Не получилось"
    return (
        "<!doctype html><meta charset=utf-8><title>FlowPilot</title>"
        "<body style='font-family:system-ui;padding:2em'>"
        f"<h2>{title}</h2><p>{_html(message)}</p>"
        f"<script>try{{window.opener&&window.opener.postMessage({payload},'*')}}catch(e){{}}"
        f"{'setTimeout(()=>window.close(),800)' if ok else ''}</script>"
    )


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
