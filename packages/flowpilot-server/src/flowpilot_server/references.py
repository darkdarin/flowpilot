"""References from node settings to things a host knows about (Home Assistant entities, services).

A node marks a config property with ``"x-flowpilot-options": "<kind>"``, e.g. ``ha.stt`` for an STT
entity id or ``wyoming.asr`` for a Wyoming service URI. The runtime then

- asks connected clients that offer :data:`HOST_OP_OPTIONS` what exists (the editor shows it as a
  list and warns about values the host does not know; warnings never block a deploy), and
- tells clients which references the deployed graphs use (``references`` message), so Home
  Assistant can raise a repair issue when a referenced entity disappears.

Result of the host operation: ``{"options": {kind: [{"id": ..., "name": ...}, ...]},
"strict": [kind, ...]}``. Options of a strict kind are everything that can work (an id outside the
list is a warning); other kinds are only suggestions (e.g. Wyoming services Home Assistant knows,
while FlowPilot can reach others directly).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from flowpilot_core import NodeRegistry
from flowpilot_sdk import OPTIONS_KEY

HOST_OP_OPTIONS = "ha.entities.list"
OPTIONS_CACHE_S = 10.0  # host option lists are asked again after this


@dataclass(frozen=True, slots=True)
class Reference:
    node: str
    index: int  # position in the graph's node list
    field: str
    kind: str
    id: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        del data["index"]
        return data


def references(data: Any, registry: NodeRegistry) -> list[Reference]:
    """Every marked setting with a string value in graph JSON (drafts may be invalid)."""
    nodes = data.get("nodes") if isinstance(data, Mapping) else None
    found: list[Reference] = []
    for index, node in enumerate(nodes if isinstance(nodes, list) else []):
        if not isinstance(node, Mapping):
            continue
        spec = registry.spec(str(node.get("type")))
        config = node.get("config")
        if spec is None or not isinstance(config, Mapping):
            continue
        props = spec.config_schema.get("properties") or {}
        for name, schema in props.items():
            kind = schema.get(OPTIONS_KEY) if isinstance(schema, Mapping) else None
            value = config.get(name)
            if isinstance(kind, str) and isinstance(value, str) and value:
                found.append(Reference(str(node.get("id")), index, name, kind, value))
    return found


def merge_options(results: list[Any]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Options from several hosts, de-duplicated by id, and the kinds any host checks strictly."""
    merged: dict[str, dict[str, dict[str, str]]] = {}
    strict: set[str] = set()
    for result in results:
        if not isinstance(result, Mapping):
            continue
        strict.update(str(k) for k in result.get("strict") or [])
        for kind, items in (result.get("options") or {}).items():
            bucket = merged.setdefault(str(kind), {})
            for item in items if isinstance(items, list) else []:
                if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                    bucket.setdefault(
                        item["id"], {"id": item["id"], "name": str(item.get("name") or item["id"])}
                    )
    options = {
        kind: sorted(items.values(), key=lambda i: i["id"]) for kind, items in merged.items()
    }
    return options, sorted(strict & set(options))


def reference_warnings(
    data: Any,
    registry: NodeRegistry,
    options: Mapping[str, list[dict[str, str]]],
    strict: Iterable[str],
) -> list[dict[str, Any]]:
    """Warnings for references of strict kinds to things the connected hosts do not list."""
    strict = set(strict)
    warnings = []
    for ref in references(data, registry):
        if ref.kind not in strict or any(o["id"] == ref.id for o in options.get(ref.kind, [])):
            continue
        warnings.append(
            {
                "code": "unknown_reference",
                "path": f"$.nodes[{ref.index}].config.{ref.field}",
                "node": ref.node,
                "field": ref.field,
                "message": f"{ref.id} is not known to the connected host ({ref.kind})",
            }
        )
    return warnings
