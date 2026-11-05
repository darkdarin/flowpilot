"""Human-readable texts of nodes and their translations.

English lives in the spec itself: ``NodeSpec.title``/``description``, ``PortSpec.description`` and
the ``title``/``description`` keywords of the config schema properties. Other languages come from
JSON files shipped inside the plugin package, one per language, covering all nodes of the package::

    my_plugin/translations/ru.json
    {
      "my.node": {
        "title": "...",
        "description": "...",
        "inputs": {"audio": "..."},
        "outputs": {"transcript": "..."},
        "config": {"uri": {"title": "...", "description": "..."},
                   "mode": {"title": "...", "options": {"fast": "..."}}}
      }
    }

Every key is optional; missing texts fall back to English. Load them with
``translations=node_translations(__package__, "my.node")`` in the spec; ``NodeSpec`` rejects
translations of ports or settings the node does not have.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True, slots=True)
class FieldText:
    title: str | None = None
    description: str | None = None
    # labels of an enum setting's values; English ones are ENUM_TITLES_KEY in the config schema
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NodeText:
    """Texts of one node in one language."""

    title: str | None = None
    description: str | None = None
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    config: Mapping[str, FieldText] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NodeText:
        unknown = set(data) - {"title", "description", "inputs", "outputs", "config"}
        if unknown:
            raise ValueError(f"unknown keys in node texts: {sorted(unknown)}")
        config = {
            name: FieldText(
                title=value.get("title"),
                description=value.get("description"),
                options=dict(value.get("options") or {}),
            )
            for name, value in (data.get("config") or {}).items()
        }
        return cls(
            title=data.get("title"),
            description=data.get("description"),
            inputs=dict(data.get("inputs") or {}),
            outputs=dict(data.get("outputs") or {}),
            config=config,
        )

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.title:
            data["title"] = self.title
        if self.description:
            data["description"] = self.description
        if self.inputs:
            data["inputs"] = dict(self.inputs)
        if self.outputs:
            data["outputs"] = dict(self.outputs)
        if self.config:
            data["config"] = {name: _field_dict(f) for name, f in self.config.items()}
        return data


def _field_dict(text: FieldText) -> dict[str, Any]:
    data: dict[str, Any] = {
        k: v for k, v in (("title", text.title), ("description", text.description)) if v
    }
    if text.options:
        data["options"] = dict(text.options)
    return data


@cache
def _package_translations(package: str) -> dict[str, dict[str, Any]]:
    folder = resources.files(package).joinpath("translations")
    if not folder.is_dir():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in folder.iterdir():
        if not entry.name.endswith(".json"):
            continue
        language = entry.name.removesuffix(".json")
        if not LANGUAGE_RE.match(language):
            raise ValueError(f"{package}: translation file {entry.name!r} is not a language code")
        data = json.loads(entry.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{package}/translations/{entry.name} must be an object of node types")
        result[language] = data
    return result


def node_translations(package: str | None, node_type: str) -> dict[str, NodeText]:
    """Translations of ``node_type`` from ``<package>/translations/*.json``, by language."""
    if package is None:
        raise ValueError("node_translations needs the package name (pass __package__)")
    return {
        language: NodeText.from_dict(nodes[node_type])
        for language, nodes in sorted(_package_translations(package).items())
        if node_type in nodes
    }
