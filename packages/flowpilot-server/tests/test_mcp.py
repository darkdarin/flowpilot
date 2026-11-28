"""MCP servers of the runtime (ADR-025): the store, the connections and the editor's API, against a
real MCP server of the official SDK over Streamable HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
import uvicorn
from aiohttp.test_utils import TestServer
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ToolAnnotations

from flowpilot_sdk import McpUnavailable
from flowpilot_server import mcp_hub
from flowpilot_server.__main__ import build_registry
from flowpilot_server.manager import GraphManager, GraphStore
from flowpilot_server.mcp_hub import McpHub, OAuthSettings, SignInNeeded
from flowpilot_server.mcp_store import McpStore
from flowpilot_server.server import FlowPilotServer

TOKEN = "secret-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def weather_server() -> MCPServer:
    server: MCPServer = MCPServer("weather")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def forecast(city: str) -> str:
        """The weather in a city today."""
        return f"Sunny in {city}"

    @server.tool()
    def forget_city(city: str) -> str:
        """Forget a saved city."""
        return f"forgot {city}"

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def learn(ctx: Context) -> str:
        """Learn the hourly forecast (adds a tool)."""

        def hourly(city: str) -> str:
            """The weather in a city by the hour."""
            return f"{city}: sunny all day"

        server.add_tool(hourly)
        await ctx.notify_tools_changed()
        return "learned"

    return server


def with_token(app: Any) -> Any:
    async def guarded(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await app(scope, receive, send)

    return guarded


@contextlib.asynccontextmanager
async def serving(port: int, *, token: bool = False) -> AsyncIterator[str]:
    app = weather_server().streamable_http_app()
    config = uvicorn.Config(
        with_token(app) if token else app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def until(check: Callable[[], bool], timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("did not happen")


def state(hub: McpHub) -> str:
    [server] = hub.servers()
    return server.state


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[McpHub]:
    hub = McpHub(McpStore(tmp_path / "mcp.json"))
    await hub.start()
    yield hub
    await hub.stop()


async def test_a_server_gives_its_tools_and_runs_them(hub: McpHub) -> None:
    async with serving(free_port()) as url:
        server = await hub.add({"name": "weather", "title": "Погода", "url": url})
        await until(lambda: state(hub) == "ready")
        tools = {t.name: t for t in hub.tools()}
        assert set(tools) == {"forecast", "forget_city", "learn"}
        assert tools["forecast"].read_only and not tools["forecast"].destructive
        assert tools["forget_city"].destructive  # no annotations: may change things
        assert tools["forecast"].parameters["properties"]["city"]["type"] == "string"
        result = await hub.call(server.id, "forecast", {"city": "Барселона"}, timeout_s=5)
        assert result.text == "Sunny in Барселона" and not result.is_error

        await hub.set_tools(server.id, ["forget_city"])
        assert "forget_city" not in {t.name for t in hub.tools()}
        with pytest.raises(McpUnavailable):
            await hub.call(server.id, "forget_city", {"city": "x"}, timeout_s=5)
        assert hub.tools(["other"]) == [] and hub.tools(["weather"])


async def test_a_new_tool_appears_when_the_server_says_so(hub: McpHub) -> None:
    async with serving(free_port()) as url:
        server = await hub.add({"name": "weather", "url": url})
        await until(lambda: state(hub) == "ready")
        assert (await hub.call(server.id, "learn", {}, timeout_s=5)).text == "learned"
        await until(lambda: "hourly" in {t.name for t in hub.tools()})


async def test_a_token_signs_in(hub: McpHub, tmp_path: Path) -> None:
    async with serving(free_port(), token=True) as url:
        server = await hub.add({"name": "weather", "url": url, "auth": "token", "token": "wrong"})
        await until(lambda: state(hub) == "error")
        assert "HTTP 401" in (hub.servers()[0].error or "")
        await hub.update(
            server.id, {"name": "weather", "url": url, "auth": "token", "token": TOKEN}
        )
        await until(lambda: state(hub) == "ready")
    public = hub.public()[0]
    assert public["token_set"] and TOKEN not in str(public)
    assert stat.S_IMODE((tmp_path / "mcp.json").stat().st_mode) == 0o600
    assert McpStore(tmp_path / "mcp.json").secrets(server.id).token == TOKEN


async def test_a_server_that_comes_back_is_connected_again(
    hub: McpHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_hub, "BACKOFF_S", (0.1,))
    port = free_port()
    server = await hub.add({"name": "weather", "url": f"http://127.0.0.1:{port}/mcp"})
    await until(lambda: state(hub) == "error")
    with pytest.raises(McpUnavailable):
        await hub.call(server.id, "forecast", {"city": "x"}, timeout_s=1)
    async with serving(port):
        await until(lambda: state(hub) == "ready")
        assert hub.tools()


async def test_an_oauth_server_waits_for_a_person(hub: McpHub) -> None:
    await hub.add({"name": "notes", "url": "https://mcp.example.com/mcp", "auth": "oauth"})
    await until(lambda: state(hub) == "needs_auth")
    assert hub.tools() == []
    plain = await hub.add({"name": "plain", "url": "https://mcp.example.com/plain", "auth": "none"})
    with pytest.raises(SignInNeeded):
        await hub.login(plain.id)
    assert not hub.callback("unknown-state", "code", None)


def test_bad_servers_are_refused(tmp_path: Path) -> None:
    store = McpStore(tmp_path / "mcp.json")
    for bad in (
        {"name": "x", "url": "ftp://example.com"},
        {"name": "Bad Name", "url": "https://example.com"},
        {"name": "x", "url": "https://example.com", "transport": "ws"},
        {"name": "x", "url": "https://example.com", "auth": "basic"},
    ):
        with pytest.raises(ValueError):
            store.add(bad)
    store.add({"name": "x", "url": "https://example.com"})
    with pytest.raises(ValueError):
        store.add({"name": "x", "url": "https://example.org"})


async def test_changing_the_provider_forgets_its_sign_in(tmp_path: Path) -> None:
    store = McpStore(tmp_path / "mcp.json")
    server = store.add({"name": "notes", "url": "https://a.example.com/mcp", "auth": "oauth"})
    store.save_oauth(server.id, client={"client_id": "c"}, tokens={"access_token": "t"})
    _, reconnect = store.update(server.id, {"name": "notes", "url": "https://a.example.com/mcp",
                                            "auth": "oauth", "title": "Заметки"})  # fmt: skip
    assert not reconnect and store.secrets(server.id).oauth_tokens
    _, reconnect = store.update(
        server.id, {"name": "notes", "url": "https://b.example.com/mcp", "auth": "oauth"}
    )
    assert reconnect and store.secrets(server.id).oauth_tokens is None
    assert McpStore(tmp_path / "mcp.json").secrets(server.id).oauth_client is None


Call = Callable[..., Awaitable[aiohttp.ClientResponse]]


async def test_the_editor_manages_servers(tmp_path: Path) -> None:
    store = GraphStore(tmp_path)
    hub = McpHub(McpStore(tmp_path / "mcp.json"))
    manager = GraphManager(build_registry(dev=True), store=store, mcp=hub)
    await hub.start()
    server = TestServer(FlowPilotServer(token="t", default_graph="main", manager=manager).app())
    await server.start_server()
    url = server.make_url
    try:
        async with (
            serving(free_port(), token=True) as mcp_url,
            aiohttp.ClientSession(headers={"Authorization": "Bearer t"}) as http,
        ):
            body = {"name": "weather", "url": mcp_url, "auth": "token", "token": TOKEN}
            made = await (await http.post(url("/api/editor/mcp"), json=body)).json()
            assert made["token_set"] and "token" not in made
            assert (await http.post(url("/api/editor/mcp"), json={"name": "?"})).status == 400
            await until(lambda: state(hub) == "ready")
            listed = await (await http.get(url("/api/editor/mcp"))).json()
            [entry] = listed["servers"]
            assert entry["state"] == "ready" and TOKEN not in str(listed)
            assert {t["name"] for t in entry["tools"]} == {"forecast", "forget_city", "learn"}
            tools_url = url(f"/api/editor/mcp/{made['id']}/tools")
            await http.put(tools_url, json={"disabled": ["learn"]})
            listed = await (await http.get(url("/api/editor/mcp"))).json()
            off = [t["name"] for t in listed["servers"][0]["tools"] if not t["enabled"]]
            assert off == ["learn"]
            login = await http.post(url(f"/api/editor/mcp/{made['id']}/login"))
            assert login.status == 409  # signs in with a token, not OAuth
            async with aiohttp.ClientSession() as anonymous:  # the provider's answer: no token
                page = await anonymous.get(url("/api/mcp/oauth/callback?state=x&code=y"))
                assert page.status == 200 and "устарел" in await page.text()
                assert (await anonymous.get(url("/api/editor/mcp"))).status == 401
            assert (await http.delete(url(f"/api/editor/mcp/{made['id']}"))).status == 200
            assert (await (await http.get(url("/api/editor/mcp"))).json())["servers"] == []
    finally:
        await server.close()
        await manager.stop()
        await hub.stop()


class Provider:
    """A tiny OAuth authorization server for the MCP server of the test: dynamic registration,
    and a person who always agrees."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        code = secrets.token_hex(8)
        assert client.client_id is not None
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300,
            client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )  # fmt: skip
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        del self.codes[authorization_code.code]
        return self._issue(authorization_code.client_id, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        del self.refresh[refresh_token.token]
        return self._issue(refresh_token.client_id, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.tokens.pop(token.token, None)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        raise NotImplementedError

    def _issue(self, client_id: str, resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_hex(16), secrets.token_hex(16)
        self.tokens[access] = AccessToken(
            token=access, client_id=client_id, scopes=[], expires_at=int(time.time()) + 3600,
            resource=resource,
        )  # fmt: skip
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=[])
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=3600)


@contextlib.asynccontextmanager
async def serving_oauth(port: int, provider: Provider) -> AsyncIterator[str]:
    base = f"http://127.0.0.1:{port}"
    server: MCPServer = MCPServer(
        "notes",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,  # type: ignore[arg-type]
            resource_server_url=f"{base}/mcp",  # type: ignore[arg-type]
            client_registration_options=ClientRegistrationOptions(enabled=True),
            validate_token_resource=True,  # the token is for this server (RFC 8707)
        ),
    )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def notes() -> str:
        """Today's notes."""
        return "buy milk"

    config = uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=1,
    )  # fmt: skip
    web_server = uvicorn.Server(config)
    task = asyncio.create_task(web_server.serve())
    await until(lambda: web_server.started)
    try:
        yield f"{base}/mcp"
    finally:
        web_server.should_exit = True
        await task


async def test_a_person_signs_in_with_oauth_once(tmp_path: Path) -> None:
    oauth = OAuthSettings(redirect_url="http://localhost:9/return", client_metadata_url=None)
    provider = Provider()
    async with serving_oauth(free_port(), provider) as url:
        hub = McpHub(McpStore(tmp_path / "mcp.json"), oauth)
        await hub.start()
        try:
            server = await hub.add({"name": "notes", "url": url, "auth": "oauth"})
            await until(lambda: state(hub) == "needs_auth")  # no tokens: waits, does not register
            assert provider.clients == {}
            address = await hub.login(server.id)
            assert address.startswith(url.removesuffix("/mcp")) and "code_challenge=" in address
            async with aiohttp.ClientSession() as browser:  # the person agrees at the provider
                answer = await browser.get(address, allow_redirects=False)
            back = urlsplit(answer.headers["Location"])
            assert back.netloc == "localhost:9"  # to the return page
            query = parse_qs(back.query)
            assert hub.callback(query["state"][0], query["code"][0], None)
            assert not hub.callback(query["state"][0], query["code"][0], None)  # once
            await until(lambda: state(hub) == "ready")
            result = await hub.call(server.id, "notes", {}, timeout_s=5)
            assert result.text == "buy milk"
        finally:
            await hub.stop()
        kept = McpStore(tmp_path / "mcp.json").secrets(server.id)
        assert kept.oauth_tokens and kept.oauth_client
        assert "access_token" not in str(hub.public())

        again = McpHub(McpStore(tmp_path / "mcp.json"), oauth)  # after a restart: no person
        await again.start()
        try:
            await until(lambda: state(again) == "ready")
            await again.sign_out(server.id)
            await until(lambda: state(again) == "needs_auth")
        finally:
            await again.stop()


async def test_a_refused_sign_in_is_shown(tmp_path: Path) -> None:
    oauth = OAuthSettings(redirect_url="http://localhost:9/return", client_metadata_url=None)
    async with serving_oauth(free_port(), Provider()) as url:
        hub = McpHub(McpStore(tmp_path / "mcp.json"), oauth)
        await hub.start()
        try:
            server = await hub.add({"name": "notes", "url": url, "auth": "oauth"})
            address = await hub.login(server.id)
            state_ = parse_qs(urlsplit(address).query)["state"][0]
            assert hub.callback(state_, None, "access_denied")
            await until(lambda: state(hub) == "needs_auth")
            assert "access_denied" in (hub.servers()[0].error or "")
        finally:
            await hub.stop()
