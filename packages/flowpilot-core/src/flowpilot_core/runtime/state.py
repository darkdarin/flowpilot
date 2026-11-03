"""In-memory session state (contracts v1: sessions do not survive a runtime restart)."""

from __future__ import annotations

import time
from typing import Any


class MemorySessionStore:
    """Session key-value state with optional TTL, namespaced per node type."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], tuple[Any, float | None]] = {}

    def view(self, session_id: str, namespace: str) -> SessionStateView:
        return SessionStateView(self, session_id, namespace)

    def drop_session(self, session_id: str) -> None:
        for key in [k for k in self._data if k[0] == session_id]:
            del self._data[key]

    def _get(self, key: tuple[str, str, str]) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and time.monotonic() >= expires:
            del self._data[key]
            return None
        return value

    def _set(self, key: tuple[str, str, str], value: Any, ttl_s: float | None) -> None:
        expires = None if ttl_s is None else time.monotonic() + ttl_s
        self._data[key] = (value, expires)

    def _delete(self, key: tuple[str, str, str]) -> None:
        self._data.pop(key, None)


class SessionStateView:
    """The ``SessionState`` a node sees: its own namespace in one session."""

    def __init__(self, store: MemorySessionStore, session_id: str, namespace: str) -> None:
        self._store = store
        self._prefix = (session_id, namespace)

    async def get(self, key: str) -> Any | None:
        return self._store._get((*self._prefix, key))

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        self._store._set((*self._prefix, key), value, ttl_s)

    async def delete(self, key: str) -> None:
        self._store._delete((*self._prefix, key))
