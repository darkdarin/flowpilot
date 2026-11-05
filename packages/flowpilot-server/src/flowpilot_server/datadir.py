"""A self-contained data directory for container installs (Docker, Home Assistant add-on).

``flowpilot-server --data /data`` creates on first start::

    /data/config.json   runtime config (edit to change ports, default graph, grants)
    /data/token         access token for clients and the editor (mode 600)
    /data/graphs/       graphs; a starter graph ``main`` that answers with a Home Assistant agent

Inside a Home Assistant add-on (``SUPERVISOR_TOKEN`` is set) the config also opens the Ingress
port, trusted for requests from the Supervisor, and add-on options from ``/data/options.json``
(``log_level``, ``dev_nodes``) apply on every start.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

PORT = 8765
INGRESS_PORT = 8099
SUPERVISOR_IP = "172.30.32.2"

STARTER_GRAPH: dict[str, Any] = {
    "schema_version": 1,
    "id": "main",
    "name": "Home Assistant agent",
    "nodes": [
        {"id": "in", "type": "client.source"},
        {"id": "agent", "type": "ha.conversation"},
        {"id": "out", "type": "client.sink"},
    ],
    "edges": [
        {"from": "in.utterance", "to": "agent.utterance"},
        {"from": "agent.delta", "to": "out.delta"},
        {"from": "agent.response", "to": "out.response"},
    ],
    "ui": {
        "nodes": {
            "in": {"x": 55, "y": 55},
            "agent": {"x": 385, "y": 55},
            "out": {"x": 715, "y": 55},
        }
    },
}


def in_addon() -> bool:
    return "SUPERVISOR_TOKEN" in os.environ


def init_data_dir(data: Path, *, addon: bool | None = None) -> Path:
    """Create what is missing in ``data`` and return the config path. Existing files are kept."""
    addon = in_addon() if addon is None else addon
    data.mkdir(parents=True, exist_ok=True)

    token = data / "token"
    if not token.exists():
        token.touch(mode=0o600)
        token.write_text(secrets.token_urlsafe(32) + "\n")
        _LOGGER.info("created an access token in %s", token)

    config_path = data / "config.json"
    if not config_path.exists():
        config: dict[str, Any] = {
            "host": "0.0.0.0",
            "port": PORT,
            "token_file": "token",
            "graphs_dir": "graphs",
            "default_graph": "main",
        }
        if addon:
            config["ingress"] = {"port": INGRESS_PORT, "trusted_peers": [SUPERVISOR_IP]}
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        _LOGGER.info("created %s", config_path)

    graphs = data / "graphs"
    graphs.mkdir(exist_ok=True)
    if not any(graphs.glob("*.json")):
        (graphs / "main.json").write_text(json.dumps(STARTER_GRAPH, indent=2) + "\n")
        _LOGGER.info("created the starter graph %s", graphs / "main.json")
    return config_path


def addon_options(data: Path) -> dict[str, Any]:
    """Options the user set on the add-on page (``/data/options.json``); empty outside add-ons."""
    path = data / "options.json"
    if not path.exists():
        return {}
    options = json.loads(path.read_text())
    return options if isinstance(options, dict) else {}
