"""Graph storage, drafts and live redeployment.

The editor saves drafts; ``deploy`` validates a draft, starts a new deployment and swaps it in
atomically. Turns already running keep the deployment they started on; the old deployment stops
once its last turn has finished.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from flowpilot_core import (
    GrantPolicy,
    GraphError,
    GraphIssue,
    NodeRegistry,
    ValidatedGraph,
    parse_graph,
    validate_graph,
)
from flowpilot_core.runtime import Deployment, MemorySessionStore, TraceSink
from flowpilot_sdk import registered_types
from flowpilot_sdk.types import PRIVILEGED_TYPES

from .memory_store import MemoryStore
from .migrations import migrate_graph
from .secret_store import secret_fields

_LOGGER = logging.getLogger(__name__)

GRAPH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
RETIRE_TIMEOUT_S = 120.0

GraphCheck = Callable[[ValidatedGraph], list[GraphIssue]]
TraceFactory = Callable[[str], TraceSink]


class DeployError(Exception):
    """A valid graph could not be started (e.g. a node failed to start or a secret is missing)."""


class GraphStore:
    """One directory: ``<id>.json`` is the deployed graph, ``<id>.draft.json`` its draft."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def ids(self) -> list[str]:
        return sorted(
            p.stem
            for p in self.directory.glob("*.json")
            if not p.name.endswith(".draft.json") and GRAPH_ID_RE.match(p.stem)
        )

    def _path(self, graph_id: str, *, draft: bool = False) -> Path:
        if not GRAPH_ID_RE.match(graph_id):
            raise ValueError(f"invalid graph id {graph_id!r}")
        return self.directory / (f"{graph_id}.draft.json" if draft else f"{graph_id}.json")

    def load(self, graph_id: str, *, draft: bool = False) -> Any | None:
        path = self._path(graph_id, draft=draft)
        return migrate_graph(json.loads(path.read_text())) if path.exists() else None

    def save(self, graph_id: str, data: Any, *, draft: bool = False) -> None:
        path = self._path(graph_id, draft=draft)
        if not draft and path.exists():
            path.replace(path.with_suffix(".json.bak"))  # keep the previous deployed version
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def delete_draft(self, graph_id: str) -> None:
        self._path(graph_id, draft=True).unlink(missing_ok=True)


def graph_version(data: Any) -> str:
    """What the graph does, as a short hash: nodes, edges and settings, not its name or layout.

    Hosts key caches on it (Home Assistant's text-to-speech cache): a deploy that changes what the
    graph does gets a new version, moving nodes around in the editor does not.
    """
    body = (
        {k: data.get(k) for k in ("nodes", "edges", "settings")} if isinstance(data, dict) else {}
    )
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()[:12]


def issue_dicts(issues: list[GraphIssue], data: Any) -> list[dict[str, Any]]:
    """Issues as JSON, with the node id or edge index they point to, for the editor."""
    nodes = data.get("nodes", []) if isinstance(data, Mapping) else []
    result = []
    for i in issues:
        item: dict[str, Any] = {"code": i.code, "path": i.path, "message": i.message}
        if m := re.match(r"^\$\.nodes\[(\d+)\]", i.path):
            idx = int(m[1])
            if idx < len(nodes) and isinstance(nodes[idx], Mapping):
                item["node"] = nodes[idx].get("id")
        elif m := re.match(r"^\$\.edges\[(\d+)\]", i.path):
            item["edge"] = int(m[1])
        result.append(item)
    return result


class GraphManager:
    """Deployed graphs of one runtime, their drafts and safe redeployment."""

    def __init__(
        self,
        registry: NodeRegistry,
        *,
        store: GraphStore | None = None,
        secrets: Mapping[str, str] | None = None,
        grants: GrantPolicy | None = None,
        check: GraphCheck | None = None,
        trace_factory: TraceFactory | None = None,
        data_dir: Path | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.data_dir = data_dir
        # long-term facts for every graph (flowpilot_sdk.Memory); in memory only when not given
        self.memory = (
            memory if memory is not None else MemoryStore(None)
        )  # nodes keep their files under <data_dir>/nodes/<type>/
        # one session state for every graph and deployment: a redeploy keeps the dialogues
        self.sessions = MemorySessionStore()
        self.registry = registry
        self.store = store
        # the live mapping (a SecretStore): deploys after a change see the new values
        self.secrets: Mapping[str, str] = secrets if secrets is not None else {}
        self._grants = grants or GrantPolicy()
        self._check = check
        self._trace_factory = trace_factory
        self._on_deployed: Callable[[str], None] | None = None
        self._watching: Callable[[str], bool] | None = None
        self.deployments: dict[str, Deployment] = {}
        self.versions: dict[str, str] = {}  # graph_version() of each deployed graph
        self._active: dict[Deployment, int] = {}
        self._idle = asyncio.Condition()
        self._retiring: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @classmethod
    def adopt(cls, deployments: Mapping[str, Deployment]) -> GraphManager:
        """Wrap already started deployments (tests, embedding); no store, no editor."""
        registry = next(iter(deployments.values())).registry if deployments else NodeRegistry()
        manager = cls(registry)
        manager.deployments = dict(deployments)
        return manager

    def set_check(self, check: GraphCheck) -> None:
        self._check = check

    def set_trace(
        self,
        factory: TraceFactory,
        on_deployed: Callable[[str], None],
        watching: Callable[[str], bool] | None = None,
    ) -> None:
        """Trace sink for deployments started from now on, a hook called after each deploy, and
        whether someone watches a graph in live debug (its debug taps run only then)."""
        self._trace_factory = factory
        self._on_deployed = on_deployed
        self._watching = watching

    # --- validation -----------------------------------------------------------------------------

    def validate(self, data: Any) -> ValidatedGraph:
        """Validate graph JSON; raise ``GraphError`` with every issue (including server checks)."""
        graph = validate_graph(parse_graph(data), self.registry, grants=self._grants)
        if self._check is not None and (issues := self._check(graph)):
            raise GraphError(issues)
        return graph

    def issues(self, data: Any) -> list[dict[str, Any]]:
        try:
            self.validate(data)
        except GraphError as err:
            return issue_dicts(list(err.issues), data)
        return []

    # --- deployment -----------------------------------------------------------------------------

    async def load_all(self) -> dict[str, str]:
        """Deploy every stored graph; return the ones that failed with the reason."""
        failed: dict[str, str] = {}
        if self.store is None:
            return failed
        for graph_id in self.store.ids():
            try:
                await self.deploy_data(graph_id, self.store.load(graph_id), persist=False)
            except (GraphError, DeployError, ValueError) as err:
                failed[graph_id] = str(err)
                _LOGGER.error("graph %s not deployed: %s", graph_id, err)
        return failed

    async def deploy_data(self, graph_id: str, data: Any, *, persist: bool = True) -> Deployment:
        if not GRAPH_ID_RE.match(graph_id):
            raise ValueError(f"invalid graph id {graph_id!r}")
        graph = self.validate(data)
        if graph.graph.id != graph_id:
            raise GraphError([GraphIssue("id_mismatch", "$.id", f"graph id must be {graph_id!r}")])
        trace = self._trace_factory(graph_id) if self._trace_factory else None
        watching = self._watching
        deployment = Deployment(
            graph,
            self.registry,
            secrets=self.secrets,
            trace=trace,
            data_dir=self.data_dir,
            sessions=self.sessions,
            memory=self.memory,
            taps_active=(lambda: watching(graph_id)) if watching else None,
        )
        try:
            await deployment.start()
        except Exception as err:  # noqa: BLE001 - any start failure keeps the old deployment
            raise DeployError(f"{type(err).__name__}: {err}") from err
        async with self._lock:
            old = self.deployments.get(graph_id)
            self.deployments[graph_id] = deployment
            self.versions[graph_id] = graph_version(data)
            if persist and self.store is not None:
                self.store.save(graph_id, data)
                self.store.delete_draft(graph_id)
        _LOGGER.info("graph %s deployed (%d nodes)", graph_id, len(graph.order))
        if self._on_deployed is not None:
            self._on_deployed(graph_id)
        if old is not None:
            task = asyncio.create_task(self._retire(old), name=f"retire:{graph_id}")
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)
        return deployment

    async def secret_changed(self, name: str) -> dict[str, str | None]:
        """Redeploy the stored graphs that use secret ``name``, so their nodes start with the new
        value (or fail without it); returns ``{graph: None | error}``. A graph that fails keeps
        its running version."""
        results: dict[str, str | None] = {}
        if self.store is None:
            return results
        for graph_id in self.store.ids():
            data = self.store.load(graph_id)
            if not any(secret == name for *_, secret in secret_fields(data)):
                continue
            try:
                await self.deploy_data(graph_id, data, persist=False)
                results[graph_id] = None
            except (GraphError, DeployError, ValueError) as err:
                results[graph_id] = str(err)
                _LOGGER.warning("graph %s not redeployed after a secret change: %s", graph_id, err)
        return results

    async def deploy_draft(self, graph_id: str) -> Deployment:
        if self.store is None:
            raise DeployError("this runtime has no graph store")
        draft = self.store.load(graph_id, draft=True)
        if draft is None:
            raise DeployError(f"graph {graph_id!r} has no draft")
        return await self.deploy_data(graph_id, draft)

    async def _retire(self, deployment: Deployment) -> None:
        try:
            async with asyncio.timeout(RETIRE_TIMEOUT_S), self._idle:
                await self._idle.wait_for(lambda: self._active.get(deployment, 0) == 0)
        except TimeoutError:
            _LOGGER.warning("old deployment still busy after %ss; stopping it", RETIRE_TIMEOUT_S)
        await deployment.stop()

    # --- turn accounting ------------------------------------------------------------------------

    @contextlib.contextmanager
    def turn(self, graph_id: str) -> Iterator[Deployment]:
        """Pin the current deployment of ``graph_id`` for the duration of one turn."""
        deployment = self.deployments[graph_id]
        self._active[deployment] = self._active.get(deployment, 0) + 1
        try:
            yield deployment
        finally:
            self._active[deployment] -= 1
            if self._active[deployment] == 0:
                del self._active[deployment]
                asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._idle:
            self._idle.notify_all()

    async def stop(self) -> None:
        for task in list(self._retiring):
            task.cancel()
        await asyncio.gather(*self._retiring, return_exceptions=True)
        for deployment in self.deployments.values():
            await deployment.stop()
        self.deployments.clear()

    # --- catalog --------------------------------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        nodes = []
        for node_type in self.registry.types():
            spec = self.registry.spec(node_type)
            assert spec is not None

            def ports(p: Mapping[str, Any]) -> list[dict[str, Any]]:
                return [
                    {
                        "name": name,
                        "type": port.type,
                        "kind": port.kind,
                        "required": port.required,
                        "delivery": port.delivery,
                        "description": port.description,
                    }
                    for name, port in p.items()
                ]

            nodes.append(
                {
                    "type": spec.type,
                    "version": spec.version,
                    "title": spec.title,
                    "category": spec.category,
                    "description": spec.description,
                    "inputs": ports(spec.inputs),
                    "outputs": ports(spec.outputs),
                    "config_schema": spec.config_schema,
                    "host_ops": sorted(spec.host_ops),
                    "grants": sorted(spec.grants),
                    "turn_timeout_s": spec.turn_timeout_s,
                    "debug_tap": spec.debug_tap,
                    "fixed": spec.fixed,
                    "translations": {
                        language: text.as_dict() for language, text in spec.translations.items()
                    },
                }
            )
        types = [
            {"id": type_id, "privileged": type_id in PRIVILEGED_TYPES}
            for type_id in sorted(registered_types())
        ]
        return {"nodes": nodes, "types": types}
