"""Run the FlowPilot server.

Configuration (JSON)::

    {
      "host": "0.0.0.0",
      "port": 8765,
      "token": "...",                      # or token_file
      "graphs_dir": "graphs",              # <id>.json deployed, <id>.draft.json drafts
      "default_graph": "main",
      "secrets_file": "secrets.json",      # optional {name: value}
      "sessions_file": "sessions.json",    # optional; dialogues kept across restarts
      "grants": {"identity.resolve": ["identity.provider"]},
      "ingress": {"port": 8099, "trusted_peers": ["172.30.32.2"]}   # optional, see auth.py
    }

Relative paths are resolved against the config file's directory. ``--data DIR`` instead of a
config path creates a self-contained data directory on first start (see ``datadir.py``).

Node plugins are discovered from the ``flowpilot.nodes`` entry point group: each entry point is a
callable returning node classes.
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

from . import datadir, supervisor
from .answer import FixedAnswer
from .devnodes import DEV_NODES
from .manager import GraphManager, GraphStore
from .nodes import ClientSink, ClientSource, HaConversation, HaStt, HaTts
from .probe import DebugProbe
from .secret_store import SecretStore
from .server import FlowPilotServer
from .session_file import SessionFile

_LOGGER = logging.getLogger("flowpilot_server")

ENTRY_POINT_GROUP = "flowpilot.nodes"


def build_registry(*, dev: bool = False) -> NodeRegistry:
    registry = register_builtin(NodeRegistry())
    classes: list[type[Node]] = [
        ClientSource,
        ClientSink,
        HaConversation,
        HaStt,
        HaTts,
        DebugProbe,
        FixedAnswer,
    ]
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


async def serve(config_path: Path, *, dev: bool = False, announce: bool = False) -> None:
    base = config_path.parent
    config = _read_json(config_path)
    token = config.get("token") or (base / config["token_file"]).read_text().strip()
    secrets = SecretStore(base / config.get("secrets_file", "secrets.json"))
    grants = GrantPolicy({node: frozenset(gs) for node, gs in (config.get("grants") or {}).items()})
    manager = GraphManager(
        build_registry(dev=dev),
        store=GraphStore(base / config.get("graphs_dir", "graphs")),
        secrets=secrets,
        grants=grants,
        data_dir=base,
    )

    def remember_default(graph_id: str) -> None:
        """The editor chose another default graph: keep it in the config file."""
        current = _read_json(config_path)
        current["default_graph"] = graph_id
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2) + "\n")
        tmp.replace(config_path)

    server = FlowPilotServer(
        token=token,
        default_graph=config.get("default_graph", "main"),
        manager=manager,
        on_default_changed=remember_default,
    )
    host, port = config.get("host", "0.0.0.0"), int(config.get("port", 8765))
    dialogues = SessionFile(
        base / config.get("sessions_file", "sessions.json"), manager.sessions, server.sessions
    )
    dialogues.load()
    saving = asyncio.create_task(dialogues.run())
    runners: list[web.AppRunner] = []
    try:
        await manager.load_all()
        runners.append(web.AppRunner(server.app()))
        await runners[-1].setup()
        await web.TCPSite(runners[-1], host, port).start()
        _LOGGER.info("listening on %s:%s", host, port)
        if ingress := config.get("ingress"):
            peers = [str(p) for p in ingress.get("trusted_peers") or []]
            runners.append(web.AppRunner(server.app(trusted_peers=peers)))
            await runners[-1].setup()
            await web.TCPSite(runners[-1], host, int(ingress["port"])).start()
            _LOGGER.info("Ingress on %s:%s, trusted peers %s", host, ingress["port"], peers)
        if announce:
            await supervisor.announce(port, token)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await manager.stop()
        saving.cancel()
        try:
            dialogues.save()
        except OSError as err:
            _LOGGER.warning("dialogues not saved: %s", err)


def main() -> None:
    parser = argparse.ArgumentParser(prog="flowpilot-server")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("config", type=Path, nargs="?", help="path to the server config JSON")
    where.add_argument("--data", type=Path, help="data directory, created on first start")
    parser.add_argument("--dev", action="store_true", help="enable development-only nodes")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    options = datadir.addon_options(args.data) if args.data else {}
    level = str(options.get("log_level") or args.log_level).upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = datadir.init_data_dir(args.data) if args.data else args.config
    dev = args.dev or bool(options.get("dev_nodes"))
    asyncio.run(serve(config, dev=dev, announce=bool(args.data) and datadir.in_addon()))


if __name__ == "__main__":
    main()
