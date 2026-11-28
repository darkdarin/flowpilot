"""MCP servers of the runtime and their sign-in state, in ``mcp.json`` (ADR-025).

Kept like the skills: one JSON file next to the runtime's config (mode 600), in Home Assistant
backups. It also holds what a server needs to connect and never goes to the editor: the token of
a server signed in with a header, and the OAuth client registration and tokens.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from flowpilot_sdk import new_ulid

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
MAX_SERVERS = 30
MAX_TOKEN_CHARS = 8192

Transport = Literal["http", "sse"]
AuthKind = Literal["none", "token", "oauth"]


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    id: str
    name: str  # latin, part of tool names: mcp__<name>__<tool>
    title: str
    url: str
    transport: Transport = "http"
    auth: AuthKind = "none"
    enabled: bool = True
    disabled_tools: tuple[str, ...] = ()
    # OAuth: a client registered by hand at the provider (else CIMD or dynamic registration)
    client_id: str = ""
    scope: str = ""


@dataclass(slots=True)
class McpSecrets:
    """What connects a server and stays in the runtime."""

    token: str = ""  # auth "token": sent as "Authorization: Bearer <token>"
    client_secret: str = ""  # with a client registered by hand
    oauth_client: dict[str, Any] | None = None  # the provider's client information
    oauth_tokens: dict[str, Any] | None = None  # access and refresh tokens


def server_from(data: Any, server_id: str | None = None) -> McpServerConfig:
    """A server from its JSON form (the editor's, the file's); ``ValueError`` when invalid."""
    if not isinstance(data, dict):
        raise ValueError("a server is an object")
    url = str(data.get("url") or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("the server's address must be an http(s) URL")
    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(
            "the name is up to 24 small latin letters, digits and _, starting with a letter"
        )
    transport: Transport = data.get("transport") or "http"
    if transport not in ("http", "sse"):
        raise ValueError("the transport is http (Streamable HTTP) or sse")
    auth: AuthKind = data.get("auth") or "none"
    if auth not in ("none", "token", "oauth"):
        raise ValueError("the sign-in is none, token or oauth")
    disabled = data.get("disabled_tools") or ()
    if not isinstance(disabled, list | tuple):
        raise ValueError("disabled_tools is a list of tool names")
    return McpServerConfig(
        id=server_id or str(data.get("id") or new_ulid()),
        name=name,
        title=" ".join(str(data.get("title") or name).split())[:80],
        url=url,
        transport=transport,
        auth=auth,
        enabled=bool(data.get("enabled", True)),
        disabled_tools=tuple(sorted({str(t) for t in disabled})),
        client_id=str(data.get("client_id") or "").strip(),
        scope=" ".join(str(data.get("scope") or "").split()),
    )


class McpStore:
    """Servers and their secrets, written through to ``path`` (mode 600); in memory if ``None``."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._servers: dict[str, McpServerConfig] = {}
        self._secrets: dict[str, McpSecrets] = {}
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            for item in data.get("servers", []) if isinstance(data, dict) else []:
                try:
                    server = server_from(item)
                except ValueError:
                    continue
                self._servers[server.id] = server
                secrets = item.get("secrets") if isinstance(item.get("secrets"), dict) else {}
                self._secrets[server.id] = McpSecrets(
                    token=str(secrets.get("token") or ""),
                    client_secret=str(secrets.get("client_secret") or ""),
                    oauth_client=secrets.get("oauth_client"),
                    oauth_tokens=secrets.get("oauth_tokens"),
                )

    def all(self) -> list[McpServerConfig]:
        return list(self._servers.values())

    def get(self, server_id: str) -> McpServerConfig | None:
        return self._servers.get(server_id)

    def secrets(self, server_id: str) -> McpSecrets:
        return self._secrets.setdefault(server_id, McpSecrets())

    def add(self, data: Any) -> McpServerConfig:
        if len(self._servers) >= MAX_SERVERS:
            raise ValueError(f"there are {MAX_SERVERS} servers already")
        server = server_from(data, new_ulid())
        self._unique(server)
        self._servers[server.id] = server
        self._secrets[server.id] = McpSecrets()
        self._set_given(server.id, data)
        self._save()
        return server

    def update(self, server_id: str, data: Any) -> tuple[McpServerConfig, bool]:
        """The changed server, and whether how it connects changed (then sign in again)."""
        old = self._servers.get(server_id)
        if old is None:
            raise KeyError(server_id)
        server = replace(server_from(data, server_id), id=server_id)
        self._unique(server)
        reconnect = (server.url, server.transport, server.auth, server.client_id, server.scope) != (
            old.url,
            old.transport,
            old.auth,
            old.client_id,
            old.scope,
        )
        secrets = self.secrets(server_id)
        if reconnect:  # another server or provider: what it gave does not apply any more
            secrets.oauth_client, secrets.oauth_tokens = None, None
        self._servers[server_id] = server
        reconnect |= self._set_given(server_id, data)
        self._save()
        return server, reconnect

    def set_tools(self, server_id: str, disabled: list[str]) -> McpServerConfig:
        server = self._servers[server_id]
        server = replace(server, disabled_tools=tuple(sorted(set(disabled))))
        self._servers[server_id] = server
        self._save()
        return server

    def remove(self, server_id: str) -> bool:
        if self._servers.pop(server_id, None) is None:
            return False
        self._secrets.pop(server_id, None)
        self._save()
        return True

    def sign_out(self, server_id: str) -> None:
        secrets = self.secrets(server_id)
        secrets.oauth_tokens = None
        self._save()

    def save_oauth(
        self,
        server_id: str,
        *,
        client: dict[str, Any] | None = None,
        tokens: dict[str, Any] | None = None,
    ) -> None:
        if server_id not in self._servers:
            return  # removed while signing in
        secrets = self.secrets(server_id)
        if client is not None:
            secrets.oauth_client = client
        if tokens is not None:
            secrets.oauth_tokens = tokens
        self._save()

    def public(self, server: McpServerConfig) -> dict[str, Any]:
        """The server for the editor: no secret values, only whether they are set."""
        secrets = self.secrets(server.id)
        return {
            **asdict(server),
            "disabled_tools": list(server.disabled_tools),
            "token_set": bool(secrets.token),
            "client_secret_set": bool(secrets.client_secret),
            "signed_in": bool(secrets.oauth_tokens),
        }

    def _set_given(self, server_id: str, data: Any) -> bool:
        """Secret values the editor sent (a value replaces, an empty string clears, a missing
        key keeps); whether any changed."""
        secrets = self.secrets(server_id)
        changed = False
        for key in ("token", "client_secret"):
            if key not in data or data[key] is None:
                continue
            value = str(data[key]).strip()
            if len(value) > MAX_TOKEN_CHARS:
                raise ValueError(f"{key} is too long")
            if value != getattr(secrets, key):
                setattr(secrets, key, value)
                changed = True
        return changed

    def _unique(self, server: McpServerConfig) -> None:
        if any(s.name == server.name and s.id != server.id for s in self._servers.values()):
            raise ValueError(f"there is a server named {server.name!r} already")

    def _save(self) -> None:
        if self.path is None:
            return
        servers = []
        for server in self._servers.values():
            secrets = self.secrets(server.id)
            servers.append(
                {
                    **asdict(server),
                    "disabled_tools": list(server.disabled_tools),
                    "secrets": {
                        "token": secrets.token,
                        "client_secret": secrets.client_secret,
                        "oauth_client": secrets.oauth_client,
                        "oauth_tokens": secrets.oauth_tokens,
                    },
                }
            )
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump({"version": 1, "servers": servers}, out, ensure_ascii=False, indent=2)
            out.write("\n")
        tmp.replace(self.path)
