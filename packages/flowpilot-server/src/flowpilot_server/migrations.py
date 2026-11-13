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
    data = _one_text_input(_agent_remember(_split_turn_filter(_rename_nodes(data))))
    data = _no_signal_nor_turn_filter(_principal_by_wire(_one_probe_input(data)))
    return _no_probes(data)


def _no_probes(data: dict[str, Any]) -> dict[str, Any]:
    """0.1.33 → 0.1.34: live debug's journal shows what every node passed on, text, audio and
    data, so the Debug probe is gone: its nodes and their wires leave the graph."""
    probes = {
        n.get("id") for n in data["nodes"] if isinstance(n, dict) and n.get("type") == "debug.probe"
    }
    if not probes:
        return data

    def node_of(ref: Any) -> Any:
        return ref.split(".", 1)[0] if isinstance(ref, str) else None

    data = copy.deepcopy(data)
    data["nodes"] = [
        n for n in data["nodes"] if not (isinstance(n, dict) and n.get("id") in probes)
    ]
    data["edges"] = [
        e
        for e in data.get("edges") or []
        if not (
            isinstance(e, dict)
            and (node_of(e.get("from")) in probes or node_of(e.get("to")) in probes)
        )
    ]
    ui = data.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("nodes"), dict):
        ui["nodes"] = {k: v for k, v in ui["nodes"].items() if k not in probes}
    _LOGGER.info("graph %s: Debug probes removed, the journal shows outputs now", data.get("id"))
    return data


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


# 0.1.16 → 0.1.17: a port may take any text, so Debug probe and Text converter have one text
# input instead of one per text type.
_OLD_TEXT_INPUTS = ("transcript", "utterance", "delta", "response")
_ONE_TEXT_INPUT = {"debug.probe", "text.convert"}


def _one_text_input(data: dict[str, Any]) -> dict[str, Any]:
    """Wires to the old per-type inputs go to ``text``. A probe with several gets a copy per
    extra one (each probe shows one text now); a converter keeps the first, as it used it."""
    types = {
        n["id"]: n["type"]
        for n in data["nodes"]
        if isinstance(n, dict) and n.get("type") in _ONE_TEXT_INPUT and isinstance(n.get("id"), str)
    }

    def old_input(edge: Any) -> tuple[str, str] | None:
        if not isinstance(edge, dict) or not isinstance(edge.get("to"), str):
            return None
        node, _, port = edge["to"].rpartition(".")
        return (node, port) if node in types and port in _OLD_TEXT_INPUTS else None

    if not any(old_input(e) for e in data.get("edges") or []):
        return data
    data = copy.deepcopy(data)
    taken = {n["id"] for n in data["nodes"] if isinstance(n, dict) and isinstance(n.get("id"), str)}
    wired: dict[str, list[tuple[int, str]]] = {}
    for i, edge in enumerate(data["edges"]):
        if (found := old_input(edge)) is not None:
            wired.setdefault(found[0], []).append((i, found[1]))
    dropped: set[int] = set()
    by_id = {n["id"]: n for n in data["nodes"] if isinstance(n, dict) and n.get("id") in types}
    ui_nodes = (data.get("ui") or {}).get("nodes") or {}
    for node_id, edges in wired.items():
        node = by_id[node_id]
        node.pop("version", None)
        edges.sort(key=lambda pair: _OLD_TEXT_INPUTS.index(pair[1]))
        first, *rest = edges
        data["edges"][first[0]] = {**data["edges"][first[0]], "to": f"{node_id}.text"}
        for n, (i, port) in enumerate(rest, start=1):
            if types[node_id] != "debug.probe":
                dropped.add(i)
                _LOGGER.warning(
                    "graph %s: Text converter %s had several inputs; kept %s, removed %s",
                    data.get("id"),
                    node_id,
                    first[1],
                    port,
                )
                continue
            copy_id = f"{node_id}_{port}"
            while copy_id in taken:
                copy_id += "_"
            taken.add(copy_id)
            data["nodes"].append({k: v for k, v in node.items() if k != "id"} | {"id": copy_id})
            data["edges"][i] = {**data["edges"][i], "to": f"{copy_id}.text"}
            if isinstance(ui_nodes.get(node_id), dict) and {"x", "y"} <= set(ui_nodes[node_id]):
                at = ui_nodes[node_id]
                ui_nodes[copy_id] = {**at, "y": at["y"] + 230 * n}
    data["edges"] = [e for i, e in enumerate(data["edges"]) if i not in dropped]
    _LOGGER.info("graph %s: probes and converters now have one text input", data.get("id"))
    return data


# 0.1.29 → 0.1.30: Voice or text turn and Sound signal are gone. A turn filter becomes a Condition
# on spoken turns (spoken → yes, typed → no); a sound signal goes, and what triggered it goes to
# the client output's ack, which confirms the turn with the satellite's own sound.


def _no_signal_nor_turn_filter(data: dict[str, Any]) -> dict[str, Any]:
    kinds = {
        n["id"]: n["type"]
        for n in data["nodes"]
        if isinstance(n, dict) and n.get("type") in ("logic.turn_filter", "audio.signal")
    }
    if not kinds:
        return data
    data = copy.deepcopy(data)
    edges = [e for e in data.get("edges") or [] if isinstance(e, dict)]
    ports = {"text": "value", "spoken": "yes", "typed": "no"}

    def ref(value: Any) -> tuple[str, str]:
        node, _, port = str(value).rpartition(".")
        return node, port

    triggers: list[str] = []  # what played a sound: its source, to the client output's ack
    kept: list[dict[str, Any]] = []
    for edge in edges:
        (src, out), (dst, inp) = ref(edge.get("from")), ref(edge.get("to"))
        if kinds.get(dst) == "audio.signal":
            triggers.append(str(edge["from"]))
            continue
        if kinds.get(src) == "audio.signal":
            continue
        if kinds.get(src) == "logic.turn_filter":
            edge = {**edge, "from": f"{src}.{ports.get(out, out)}"}
        if kinds.get(dst) == "logic.turn_filter":
            edge = {**edge, "to": f"{dst}.{ports.get(inp, inp)}"}
        kept.append(edge)
    sink = next(
        (n["id"] for n in data["nodes"] if isinstance(n, dict) and n.get("type") == "client.sink"),
        None,
    )
    if sink and triggers and not any(e.get("to") == f"{sink}.ack" for e in kept):
        kept.append({"from": triggers[0], "to": f"{sink}.ack"})
    data["edges"] = kept
    nodes = []
    for node in data["nodes"]:
        kind = kinds.get(node.get("id")) if isinstance(node, dict) else None
        if kind == "audio.signal":
            continue
        if kind == "logic.turn_filter":
            node = {"id": node["id"], "type": "logic.condition", "config": {"turn": "voice"}}
        nodes.append(node)
    data["nodes"] = nodes
    ui_nodes = (data.get("ui") or {}).get("nodes")
    if isinstance(ui_nodes, dict):
        for node_id, kind in kinds.items():
            if kind == "audio.signal":
                ui_nodes.pop(node_id, None)
    _LOGGER.info(
        "graph %s: Voice or text turn became a Condition, Sound signal the client output's ack",
        data.get("id"),
    )
    return data


# Schema 1 → 2 (0.1.25): who acts reaches nodes only by wire, from the client input's ``principal``
# output; nodes no longer fall back to the turn's context. Graphs of 1 get the wire to every node
# that takes it and has none, so they work as before. Once: a wire removed later stays removed.
_PRINCIPAL_TAKERS = {"llm.agent", "ha.conversation", "ha.quick"}


def _principal_by_wire(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("schema_version") != 1:
        return data
    data = copy.deepcopy(data)
    data["schema_version"] = 2
    nodes = [n for n in data["nodes"] if isinstance(n, dict) and isinstance(n.get("id"), str)]
    source = next((n["id"] for n in nodes if n.get("type") == "client.source"), None)
    if source is None:
        return data
    edges = data.setdefault("edges", [])
    wired = {e.get("to") for e in edges if isinstance(e, dict)}
    added = []
    for node in nodes:
        if node.get("type") in _PRINCIPAL_TAKERS and f"{node['id']}.principal" not in wired:
            edges.append({"from": f"{source}.principal", "to": f"{node['id']}.principal"})
            added.append(node["id"])
    if added:
        _LOGGER.info("graph %s: who speaks now comes by wire to %s", data.get("id"), added)
    return data


# 0.1.23 → 0.1.24: the Debug probe has one input of any type, ``value``, instead of ``audio`` and
# ``text``.
_OLD_PROBE_INPUTS = ("text", "audio")


def _one_probe_input(data: dict[str, Any]) -> dict[str, Any]:
    """Wires to a probe's ``text`` or ``audio`` go to ``value``; a probe with both keeps the text
    and gets a copy for the audio."""
    probes = {
        n["id"]
        for n in data["nodes"]
        if isinstance(n, dict) and n.get("type") == "debug.probe" and isinstance(n.get("id"), str)
    }

    def old_input(edge: Any) -> tuple[str, str] | None:
        if not isinstance(edge, dict) or not isinstance(edge.get("to"), str):
            return None
        node, _, port = edge["to"].rpartition(".")
        return (node, port) if node in probes and port in _OLD_PROBE_INPUTS else None

    if not any(old_input(e) for e in data.get("edges") or []):
        return data
    data = copy.deepcopy(data)
    taken = {n["id"] for n in data["nodes"] if isinstance(n, dict) and isinstance(n.get("id"), str)}
    wired: dict[str, list[tuple[int, str]]] = {}
    for i, edge in enumerate(data["edges"]):
        if (found := old_input(edge)) is not None:
            wired.setdefault(found[0], []).append((i, found[1]))
    by_id = {n["id"]: n for n in data["nodes"] if isinstance(n, dict) and n.get("id") in probes}
    ui_nodes = (data.get("ui") or {}).get("nodes") or {}
    for node_id, edges in wired.items():
        node = by_id[node_id]
        node.pop("version", None)
        edges.sort(key=lambda pair: _OLD_PROBE_INPUTS.index(pair[1]))
        (first, _), *rest = edges
        data["edges"][first] = {**data["edges"][first], "to": f"{node_id}.value"}
        for i, port in rest:
            copy_id = f"{node_id}_{port}"
            while copy_id in taken:
                copy_id += "_"
            taken.add(copy_id)
            data["nodes"].append({k: v for k, v in node.items() if k != "id"} | {"id": copy_id})
            data["edges"][i] = {**data["edges"][i], "to": f"{copy_id}.value"}
            if isinstance(ui_nodes.get(node_id), dict) and {"x", "y"} <= set(ui_nodes[node_id]):
                at = ui_nodes[node_id]
                ui_nodes[copy_id] = {**at, "y": at["y"] - 230}  # above: text copies go below
    _LOGGER.info("graph %s: Debug probes now have one input of any type", data.get("id"))
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
