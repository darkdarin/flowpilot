"""Learned quick commands (``flowpilot_sdk.QuickCommands``), kept next to the memory.

One JSON file (``commands_file``, ``quick_commands.json`` by default), readable by the runtime's
user only, in Home Assistant backups with the rest of the app's data. The LLM agent adds commands
people agreed to; Quick commands replays them; the editor lists, edits and removes them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flowpilot_sdk import Action, QuickCommand, new_ulid
from flowpilot_sdk.commands import (
    MAX_ACTIONS,
    MAX_PHRASE_CHARS,
    MAX_PHRASES,
    NOT_REPLAYED,
)

MAX_COMMANDS = 500


class CommandStore:
    """Commands written through to ``path`` (mode 600) on change; in memory when ``None``."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._commands: dict[str, QuickCommand] = {}
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            for item in data.get("commands", []) if isinstance(data, dict) else []:
                try:
                    command = QuickCommand(
                        id=str(item["id"]),
                        phrases=tuple(str(p) for p in item["phrases"]),
                        actions=tuple(
                            Action(str(a["tool"]), dict(a.get("arguments") or {}))
                            for a in item["actions"]
                        ),
                        reply=str(item.get("reply") or ""),
                        created_at=str(item["created_at"]),
                        created_by=item.get("created_by"),
                        source=str(item.get("source") or "editor"),
                        uses=int(item.get("uses") or 0),
                    )
                except KeyError, TypeError, ValueError:
                    continue
                self._commands[command.id] = command

    def all(self) -> list[QuickCommand]:
        return list(self._commands.values())

    def add(
        self,
        phrases: list[str],
        actions: list[Action],
        reply: str,
        *,
        created_by: str | None,
        source: str,
    ) -> QuickCommand:
        if len(self._commands) >= MAX_COMMANDS:
            raise ValueError(f"there are {MAX_COMMANDS} commands already: remove some first")
        command = QuickCommand(
            id=new_ulid(),
            phrases=_phrases(phrases),
            actions=_actions(actions),
            reply=" ".join(str(reply).split())[:MAX_PHRASE_CHARS],
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            created_by=created_by,
            source=source,
        )
        self._commands[command.id] = command
        self._save()
        return command

    def update(self, command_id: str, *, phrases: list[str], reply: str) -> QuickCommand:
        command = replace(
            self._commands[command_id],
            phrases=_phrases(phrases),
            reply=" ".join(str(reply).split())[:MAX_PHRASE_CHARS],
        )
        self._commands[command_id] = command
        self._save()
        return command

    def remove(self, command_id: str) -> bool:
        if self._commands.pop(command_id, None) is None:
            return False
        self._save()
        return True

    def used(self, command_id: str) -> None:
        command = self._commands.get(command_id)
        if command is not None:
            self._commands[command_id] = replace(command, uses=command.uses + 1)
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        data: dict[str, Any] = {
            "version": 1,
            "commands": [asdict(c) for c in self._commands.values()],
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        tmp.replace(self.path)


def _phrases(phrases: list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for phrase in phrases:
        text = " ".join(str(phrase).split())
        if text and text.casefold() not in (p.casefold() for p in cleaned):
            cleaned.append(text)
    if not 1 <= len(cleaned) <= MAX_PHRASES or any(len(p) > MAX_PHRASE_CHARS for p in cleaned):
        raise ValueError(
            f"a command needs 1 to {MAX_PHRASES} phrases of up to {MAX_PHRASE_CHARS} characters"
        )
    return tuple(cleaned)


def _actions(actions: list[Action]) -> tuple[Action, ...]:
    if not 1 <= len(actions) <= MAX_ACTIONS:
        raise ValueError(f"a command does 1 to {MAX_ACTIONS} things")
    for action in actions:
        if not action.tool or action.tool in NOT_REPLAYED:
            raise ValueError(f"{action.tool!r} cannot be part of a quick command")
        json.dumps(dict(action.arguments))  # must be stored as JSON
    return tuple(Action(a.tool, dict(a.arguments)) for a in actions)
