"""Dialogues on disk: session state and the dialogue index survive a restart.

Both live in memory and are written to one JSON file (mode 600: it holds what people said) a few
seconds after they change, and once more on shutdown. A missing or damaged file starts empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)

SAVE_EVERY_S = 5.0
VERSION = 1


class _Saved(Protocol):
    changes: int

    def dump(self) -> list[dict[str, Any]]: ...

    def load(self, records: Any) -> None: ...


class SessionFile:
    """Keeps ``state`` (node session state) and ``index`` (dialogue keys) in ``path``."""

    def __init__(self, path: Path, state: _Saved, index: _Saved) -> None:
        self.path = path
        self._state = state
        self._index = index
        self._saved: tuple[int, int] | None = None

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("dialogues not restored from %s: %s", self.path, err)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            _LOGGER.warning("dialogues not restored from %s: unknown format", self.path)
            return
        self._index.load(data.get("index"))
        self._state.load(data.get("state"))
        self._saved = self._changes()

    def _changes(self) -> tuple[int, int]:
        return self._state.changes, self._index.changes

    def save(self) -> bool:
        """Write the file if anything changed since the last save; ``True`` when written."""
        changes = self._changes()
        if changes == self._saved:
            return False
        data = {"version": VERSION, "index": self._index.dump(), "state": self._state.dump()}
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False)
        tmp.replace(self.path)
        self._saved = changes
        return True

    async def run(self, every_s: float = SAVE_EVERY_S) -> None:
        """Save periodically until cancelled."""
        while True:
            await asyncio.sleep(every_s)
            try:
                self.save()
            except OSError as err:
                _LOGGER.warning("dialogues not saved to %s: %s", self.path, err)
