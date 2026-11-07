"""Skills people describe for the LLM agent (``flowpilot_sdk.Skills``), in ``skills.json``.

Kept like the memory and the quick commands: one JSON file next to the runtime's config (mode
600), in Home Assistant backups. The editor creates, edits and removes skills; agents offer the
enabled ones to their model.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from flowpilot_sdk import Skill, SkillAction, SkillField, new_ulid
from flowpilot_sdk.skills import check

MAX_SKILLS = 100


def skill_from(data: Any, skill_id: str | None = None) -> Skill:
    """A skill from its JSON form (the editor's, the file's); ``ValueError`` when invalid."""
    if not isinstance(data, dict):
        raise ValueError("a skill is an object")
    try:
        fields = tuple(
            SkillField(
                name=str(f["name"]),
                title=str(f.get("title") or ""),
                type=f.get("type") or "text",
                options=tuple(str(o) for o in f.get("options") or ()),
                required=bool(f.get("required", True)),
            )
            for f in data.get("fields") or []
        )
        actions = tuple(
            SkillAction(
                action=str(a["action"]),
                data=dict(a.get("data") or {}),
                target=dict(a.get("target") or {}),
            )
            for a in data.get("actions") or []
        )
    except (KeyError, TypeError) as err:
        raise ValueError(f"bad skill: {err}") from err
    if any(f.type not in ("text", "number", "choice") for f in fields):
        raise ValueError("a field is text, number or choice")
    skill = Skill(
        id=skill_id or str(data.get("id") or new_ulid()),
        name=str(data.get("name") or ""),
        description=" ".join(str(data.get("description") or "").split()),
        fields=fields,
        actions=actions,
        enabled=bool(data.get("enabled", True)),
    )
    check(skill)
    return skill


class SkillStore:
    """Skills written through to ``path`` (mode 600) on change; in memory when ``None``."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._skills: dict[str, Skill] = {}
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            for item in data.get("skills", []) if isinstance(data, dict) else []:
                try:
                    skill = skill_from(item)
                except ValueError:
                    continue
                self._skills[skill.id] = skill

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def add(self, data: Any) -> Skill:
        if len(self._skills) >= MAX_SKILLS:
            raise ValueError(f"there are {MAX_SKILLS} skills already")
        skill = skill_from(data, new_ulid())
        self._unique(skill)
        self._skills[skill.id] = skill
        self._save()
        return skill

    def update(self, skill_id: str, data: Any) -> Skill:
        if skill_id not in self._skills:
            raise KeyError(skill_id)
        skill = replace(skill_from(data, skill_id), id=skill_id)
        self._unique(skill)
        self._skills[skill_id] = skill
        self._save()
        return skill

    def remove(self, skill_id: str) -> bool:
        if self._skills.pop(skill_id, None) is None:
            return False
        self._save()
        return True

    def _unique(self, skill: Skill) -> None:
        if any(s.name == skill.name and s.id != skill.id for s in self._skills.values()):
            raise ValueError(f"there is a skill named {skill.name!r} already")

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"version": 1, "skills": [asdict(s) for s in self._skills.values()]}
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        tmp.replace(self.path)
