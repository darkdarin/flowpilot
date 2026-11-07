"""Graphs saved by earlier versions, brought up to date when they are read.

Node types that were merged or renamed keep working: a graph that names them is rewritten in memory
on load (and saved in the new form with the next edit or deploy). Each step is idempotent, so a
graph is migrated the same way however often it is read.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# 0.1.2 → 0.1.3: Record audio and Show text became one Debug probe (same input names); Fixed text
# became Fixed answer (input ``after`` → ``utterance``, no ``utterance`` output, no ``when``); the
# dev echo agent became a Fixed answer that repeats the utterance.
_RENAMED = {
    "debug.record": "debug.probe",
    "debug.text_tap": "debug.probe",
    "debug.text": "debug.answer",
    "dev.echo_agent": "debug.answer",
}
_INPUTS = {"debug.text": {"after": "utterance"}}
_DROPPED_INPUTS = {"dev.echo_agent": {"principal"}}
_DROPPED_OUTPUTS = {"debug.text": {"utterance"}}


def _config(old_type: str, config: dict[str, Any]) -> dict[str, Any]:
    if old_type == "debug.text":
        return {k: v for k, v in config.items() if k != "when"}
    if old_type == "dev.echo_agent":
        return {"text": f"{config.get('prefix', 'echo: ')}{{text}}", "mode": "words"}
    return config


def migrate_graph(data: Any) -> Any:
    """``data`` brought up to date; the same object when there is nothing to change."""
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return data
    return _agent_remember(_split_turn_filter(_rename_nodes(data)))


def _split_turn_filter(data: dict[str, Any]) -> dict[str, Any]:
    """0.1.8 → 0.1.9: Voice or text turn chose its turns in ``turns``; now it has two outputs.

    The old ``text`` output becomes ``spoken`` (the default) or ``typed``, and ``turns`` goes.
    """
    nodes = {
        n["id"]: n
        for n in data["nodes"]
        if isinstance(n, dict)
        and n.get("type") == "logic.turn_filter"
        and isinstance(n.get("id"), str)
    }
    old = {
        node_id: "typed" if (node.get("config") or {}).get("turns") == "text" else "spoken"
        for node_id, node in nodes.items()
        if "turns" in (node.get("config") or {})
        or any(
            isinstance(e, dict) and e.get("from") == f"{node_id}.text"
            for e in data.get("edges") or []
        )
    }
    if not old:
        return data
    data = copy.deepcopy(data)
    for node in data["nodes"]:
        if isinstance(node, dict) and node.get("id") in old:
            node.pop("version", None)
            config = {k: v for k, v in (node.get("config") or {}).items() if k != "turns"}
            if config:
                node["config"] = config
            else:
                node.pop("config", None)
    data["edges"] = [
        {**e, "from": f"{e['from'][: -len('.text')]}.{old[e['from'][: -len('.text')]]}"}
        if isinstance(e, dict)
        and isinstance(e.get("from"), str)
        and e["from"].endswith(".text")
        and e["from"][: -len(".text")] in old
        else e
        for e in data.get("edges") or []
    ]
    _LOGGER.info("graph %s: Voice or text turn %s now has two outputs", data.get("id"), sorted(old))
    return data


def _agent_remember(data: dict[str, Any]) -> dict[str, Any]:
    """0.1.9 → 0.1.10: the LLM agent's ``tools`` (on/off) became ``remember`` (asked/auto/off)."""
    old = [
        n["id"]
        for n in data["nodes"]
        if isinstance(n, dict)
        and n.get("type") == "llm.agent"
        and isinstance(n.get("config"), dict)
        and "tools" in n["config"]
    ]
    if not old:
        return data
    data = copy.deepcopy(data)
    for node in data["nodes"]:
        if isinstance(node, dict) and node.get("id") in old:
            config = dict(node["config"])
            if config.pop("tools") is False:
                config.setdefault("remember", "off")
            node["config"] = config
    _LOGGER.info(
        "graph %s: LLM agent %s: Remember on request is now Remembers facts", data.get("id"), old
    )
    return data


def _rename_nodes(data: dict[str, Any]) -> dict[str, Any]:
    old = {
        n["id"]: n["type"]
        for n in data["nodes"]
        if isinstance(n, dict) and n.get("type") in _RENAMED and isinstance(n.get("id"), str)
    }
    if not old:
        return data
    data = copy.deepcopy(data)
    for node in data["nodes"]:
        if isinstance(node, dict) and node.get("id") in old:
            node["type"] = _RENAMED[old[node["id"]]]
            node.pop("version", None)
            config = _config(old[node["id"]], dict(node.get("config") or {}))
            if config:
                node["config"] = config
            else:
                node.pop("config", None)

    def port(ref: Any) -> tuple[str, str] | None:
        if not isinstance(ref, str) or "." not in ref:
            return None
        node, name = ref.rsplit(".", 1)
        return node, name

    edges = []
    for edge in data.get("edges") or []:
        if not isinstance(edge, dict):
            edges.append(edge)  # left to validation
            continue
        src, dst = port(edge.get("from")), port(edge.get("to"))
        if src and src[0] in old and src[1] in _DROPPED_OUTPUTS.get(old[src[0]], ()):
            continue
        if dst and dst[0] in old:
            if dst[1] in _DROPPED_INPUTS.get(old[dst[0]], ()):
                continue
            renamed = _INPUTS.get(old[dst[0]], {}).get(dst[1])
            if renamed:
                edge = {**edge, "to": f"{dst[0]}.{renamed}"}
        edges.append(edge)
    data["edges"] = edges
    _LOGGER.info(
        "graph %s: updated old nodes %s",
        data.get("id"),
        ", ".join(f"{n} ({t})" for n, t in sorted(old.items())),
    )
    return data
