"""Shared bits of the Soniox nodes: endpoints, errors, the API key and model lists."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp

from flowpilot_sdk import ConfigOption

STT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
TTS_URL = "wss://tts-rt.soniox.com/tts-websocket"
API_URL = "https://api.soniox.com/v1"
HTTP_TIMEOUT_S = 15.0

# https://docs.soniox.com/stt/concepts/supported-languages
LANGUAGES = [
    "af", "ar", "az", "be", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "es", "et",
    "eu", "fa", "fi", "fr", "gl", "gu", "he", "hi", "hr", "hu", "id", "it", "ja", "kk", "kn", "ko",
    "lt", "lv", "mk", "ml", "mr", "ms", "nl", "no", "pa", "pl", "pt", "ro", "ru", "sk", "sl", "sq",
    "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi", "zh",
]  # fmt: skip

API_KEY_SETTING: dict[str, Any] = {
    "title": "API key",
    "description": "Your Soniox API key, kept as a runtime secret (Secrets in the editor).",
    "type": "secret",
}


class SonioxError(Exception):
    """Soniox returned an error or the connection failed."""

    def __init__(self, message: str, error_type: str = "connection_error") -> None:
        super().__init__(message)
        self.error_type = error_type


def error_of(data: Mapping[str, Any]) -> SonioxError | None:
    """The error a Soniox WebSocket message reports, if any."""
    if "error_code" not in data and "error_type" not in data:
        return None
    error_type = str(data.get("error_type") or data.get("error_code"))
    return SonioxError(
        f"{data.get('error_code')} {error_type}: {data.get('error_message')} "
        f"(request_id={data.get('request_id')})",
        error_type=error_type,
    )


def language_base(language: str | None) -> str | None:
    """``ru-RU`` → ``ru`` when Soniox knows it; ``None`` otherwise."""
    if not language:
        return None
    base = language.replace("_", "-").split("-")[0].lower()
    return base if base in LANGUAGES else None


def api_key_of(config: Mapping[str, Any]) -> str | None:
    """The key in a config whose secret reference the runtime resolved (setting choices)."""
    key = config.get("api_key")
    return key if isinstance(key, str) and key else None


async def get_json(path: str, api_key: str) -> Any:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f"{API_URL}/{path}", headers={"Authorization": f"Bearer {api_key}"}
        ) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise SonioxError(f"HTTP {resp.status}: {body}", error_type=str(resp.status))
            return await resp.json()


def options(items: list[tuple[str, str]]) -> list[ConfigOption]:
    return [ConfigOption(id=i, name=n) for i, n in items]
