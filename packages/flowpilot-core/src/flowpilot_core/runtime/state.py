"""Session state: key-value data that outlives a turn, namespaced per node type.

The store lives in memory; an adapter may save it with ``dump`` and bring it back with ``load``
(the server keeps it on disk, so dialogues survive a restart). Expiry is wall-clock time for that
reason. ``changes`` grows with every write, so a saver knows when there is something to save.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


class MemorySessionStore:
    """Session key-value state with optional TTL, namespaced per node type."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str, str], tuple[Any, float | None]] = {}
        self.changes = 0

    def view(self, session_id: str, namespace: str) -> SessionStateView:
        return SessionStateView(self, session_id, namespace)

    def drop_session(self, session_id: str) -> None:
        for key in [k for k in self._data if k[0] == session_id]:
            del self._data[key]
        self.changes += 1

    def dump(self) -> list[dict[str, Any]]:
        """The live entries as JSON-ready records; values that are not JSON are left out."""
        now = time.time()
        records = []
        for (session, namespace, key), (value, expires) in self._data.items():
            if expires is not None and expires <= now:
                continue
            try:
                json.dumps(value)
            except TypeError, ValueError:
                _LOGGER.debug(
                    "session value %s/%s/%s is not JSON; not saved", session, namespace, key
                )
                continue
            records.append(
                {
                    "session": session,
                    "ns": namespace,
                    "key": key,
                    "value": value,
                    "expires": expires,
                }
            )
        return records

    def load(self, records: Any) -> None:
        """Entries saved by ``dump``; expired and malformed ones are skipped."""
        now = time.time()
        for record in records if isinstance(records, list) else []:
            try:
                key = (str(record["session"]), str(record["ns"]), str(record["key"]))
                expires = record.get("expires")
                if expires is not None and float(expires) <= now:
                    continue
                self._data[key] = (record["value"], None if expires is None else float(expires))
            except KeyError, TypeError, ValueError, AttributeError:
                continue

    def _get(self, key: tuple[str, str, str]) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and time.time() >= expires:
            del self._data[key]
            return None
        return value

    def _set(self, key: tuple[str, str, str], value: Any, ttl_s: float | None) -> None:
        expires = None if ttl_s is None else time.time() + ttl_s
        self._data[key] = (value, expires)
        self.changes += 1

    def _delete(self, key: tuple[str, str, str]) -> None:
        if self._data.pop(key, None) is not None:
            self.changes += 1


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
