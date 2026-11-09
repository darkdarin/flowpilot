"""Shared bits of the Deepgram nodes: endpoints, errors, the API key and languages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp

STT_URL = "wss://api.deepgram.com/v2/listen"
TTS_URL = "wss://api.deepgram.com/v2/speak"
CONNECT_TIMEOUT_S = 10.0
HEARTBEAT_S = 20.0  # pings keep a connection open while the agent is busy (idle limit: 60 s)

# https://developers.deepgram.com/docs/flux/language-prompting
STT_LANGUAGES = ["en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl"]
STT_MODELS = ["flux-general-multi", "flux-general-en"]

API_KEY_SETTING: dict[str, Any] = {
    "title": "API key",
    "description": "Your Deepgram API key, kept as a runtime secret (Secrets in the editor).",
    "type": "secret",
}


class DeepgramError(Exception):
    """Deepgram returned an error or the connection failed."""

    def __init__(self, message: str, code: str = "connection_error") -> None:
        super().__init__(message)
        self.code = code


def error_of(data: Mapping[str, Any]) -> DeepgramError | None:
    """The fatal error a Deepgram WebSocket message reports, if any."""
    if data.get("type") != "Error":
        return None
    code = str(data.get("code") or "error")
    return DeepgramError(f"{code}: {data.get('description')}", code=code)


def language_base(language: str | None) -> str | None:
    """``ru-RU`` → ``ru``; ``None`` when not set."""
    if not language:
        return None
    return language.replace("_", "-").split("-")[0].lower() or None


async def connect(
    session: aiohttp.ClientSession, url: str, params: list[tuple[str, str]], api_key: str
) -> aiohttp.ClientWebSocketResponse:
    """Open a Deepgram WebSocket; a rejected handshake becomes a ``DeepgramError``."""
    try:
        return await session.ws_connect(
            url,
            params=params,
            headers={"Authorization": f"Token {api_key}"},
            heartbeat=HEARTBEAT_S,
            compress=0,
        )
    except aiohttp.WSServerHandshakeError as err:
        hint = {401: "invalid API key", 402: "no credits left", 403: "the key may not do this"}
        reason = hint.get(err.status, err.message)
        raise DeepgramError(f"HTTP {err.status}: {reason}", code=str(err.status)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DeepgramError(f"cannot connect to Deepgram: {type(err).__name__}: {err}") from err


def http_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT_S)
    )
