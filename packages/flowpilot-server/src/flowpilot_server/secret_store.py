"""Runtime secrets: API keys and the like that node settings refer to as ``{"secret": name}``.

They live in one JSON file next to the runtime's config (``secrets_file``, ``secrets.json`` by
default), readable by the runtime's user only. The editor can set and remove them but never reads
a value back: the API only says which names are set and which graphs use them. A node gets the
values its own settings refer to, when it starts (``NodeContext.secret``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_VALUE_CHARS = 8192


class SecretStore(MutableMapping[str, str]):
    """Secret values by name, written through to ``path`` (mode 600) on every change."""

    def __init__(self, path: Path | None, initial: dict[str, str] | None = None) -> None:
        self.path = path
        self._values: dict[str, str] = dict(initial or {})
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError(f"{path} must hold an object of secret names and values")
            self._values.update({str(k): str(v) for k, v in data.items()})

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __setitem__(self, name: str, value: str) -> None:
        if not NAME_RE.match(name):
            raise ValueError(f"invalid secret name {name!r}")
        if not value or len(value) > MAX_VALUE_CHARS:
            raise ValueError("a secret value must be 1 to 8192 characters")
        self._values[name] = value
        self._save()

    def __delitem__(self, name: str) -> None:
        del self._values[name]
        self._save()

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(self._values, out, indent=2, sort_keys=True)
            out.write("\n")
        tmp.replace(self.path)


def secret_fields(data: Any) -> Iterator[tuple[int, str, str, str]]:
    """``(node index, node id, setting, secret name)`` for graph settings that name a secret."""
    nodes = data.get("nodes") if isinstance(data, dict) else None
    for index, node in enumerate(nodes if isinstance(nodes, list) else []):
        config = node.get("config") if isinstance(node, dict) else None
        if not isinstance(config, dict):
            continue
        for field, value in config.items():
            if isinstance(value, dict) and set(value) == {"secret"}:
                yield index, str(node.get("id")), field, str(value["secret"])
