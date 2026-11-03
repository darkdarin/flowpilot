"""Run the FlowPilot server.

Configuration (JSON)::

    {
      "host": "0.0.0.0",
      "port": 8765,
      "token": "...",                      # or token_file
      "graphs_dir": "graphs",              # <id>.json deployed, <id>.draft.json drafts
      "default_graph": "main",
      "secrets_file": "secrets.json",      # optional {name: value}
      "grants": {"identity.resolve": ["identity.provider"]}
    }

Relative paths are resolved against the config file's directory. Node plugins are discovered from
the ``flowpilot.nodes`` entry point group: each entry point is a callable returning node classes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from aiohttp import web

from flowpilot_core import GrantPolicy, NodeRegistry
from flowpilot_core.builtin import register_builtin
from flowpilot_sdk import Node

from .devnodes import DEV_NODES
from .manager import GraphManager, GraphStore
from .nodes import ClientSink, ClientSource, HaConversation, HaStt, HaTts
from .server import FlowPilotServer

_LOGGER = logging.getLogger("flowpilot_server")

ENTRY_POINT_GROUP = "flowpilot.nodes"


def build_registry(*, dev: bool = False) -> NodeRegistry:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [ClientSource, ClientSink, HaConversation, HaStt, HaTts]
    if dev:
        classes += DEV_NODES
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        provider: Callable[[], Iterable[type[Node]]] = ep.load()
        classes += list(provider())
        _LOGGER.info("loaded node plugin %s", ep.name)
    for cls in classes:
        registry.register(cls)
    return registry


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


async def serve(config_path: Path, *, dev: bool = False) -> None:
    base = config_path.parent
    config = _read_json(config_path)
    token = config.get("token") or (base / config["token_file"]).read_text().strip()
    secrets = _read_json(base / config["secrets_file"]) if config.get("secrets_file") else {}
    grants = GrantPolicy({node: frozenset(gs) for node, gs in (config.get("grants") or {}).items()})
    manager = GraphManager(
        build_registry(dev=dev),
        store=GraphStore(base / config.get("graphs_dir", "graphs")),
        secrets=secrets,
        grants=grants,
    )
    server = FlowPilotServer(
        token=token, default_graph=config.get("default_graph", "main"), manager=manager
    )
    try:
        await manager.load_all()
        runner = web.AppRunner(server.app())
        await runner.setup()
        site = web.TCPSite(runner, config.get("host", "0.0.0.0"), int(config.get("port", 8765)))
        await site.start()
        _LOGGER.info("listening on %s:%s", config.get("host", "0.0.0.0"), config.get("port", 8765))

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await runner.cleanup()
    finally:
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="flowpilot-server")
    parser.add_argument("config", type=Path, help="path to the server config JSON")
    parser.add_argument("--dev", action="store_true", help="enable development-only nodes")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve(args.config, dev=args.dev))


if __name__ == "__main__":
    main()
