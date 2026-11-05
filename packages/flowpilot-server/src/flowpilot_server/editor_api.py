"""REST API for the graph editor: node catalog, graphs, drafts, validation and deploy.

All routes need ``Authorization: Bearer <token>``.

- ``GET  /api/editor/catalog``                node types (ports, config schemas) and payload types
- ``GET  /api/editor/options``                what connected hosts offer for marked settings
                                              (``?refresh=1`` skips the short cache)
- ``POST /api/editor/node-options``           ``{type, field, config}``: choices a node lists itself
                                              for a setting (``NODE_OPTIONS_KEY``)
- ``GET  /api/editor/graphs``                 graphs with deploy/draft state
- ``POST /api/editor/graphs``                 ``{id, name}``: create a draft for a new graph
- ``GET  /api/editor/graphs/{id}``            deployed graph, draft and the draft's issues
- ``PUT  /api/editor/graphs/{id}/draft``      save a draft (may be invalid); returns its issues

Issues block a deploy; ``warnings`` (references to entities the host does not list) do not.
- ``DELETE /api/editor/graphs/{id}/draft``    discard the draft
- ``POST /api/editor/graphs/{id}/deploy``     validate and deploy the draft (409 no draft,
  422 invalid with issues, 500 the graph is valid but failed to start)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from flowpilot_core import GraphError

from .auth import bearer_token
from .manager import GRAPH_ID_RE, DeployError, GraphManager, GraphStore, issue_dicts
from .references import OPTIONS_CACHE_S

if TYPE_CHECKING:
    from .server import FlowPilotServer


PREFIX = "/api/editor"
NODE_OPTIONS_TIMEOUT_S = 8.0

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def new_graph(graph_id: str, name: str) -> dict[str, Any]:
    """The smallest servable graph: client input and output, to be wired up in the editor."""
    return {
        "schema_version": 1,
        "id": graph_id,
        "name": name,
        "settings": {"turn_timeout_ms": 30000},
        "nodes": [
            {"id": "in", "type": "client.source", "version": "^1", "config": {}},
            {"id": "out", "type": "client.sink", "version": "^1", "config": {}},
        ],
        "edges": [],
        "ui": {"nodes": {"in": {"x": 80, "y": 200}, "out": {"x": 720, "y": 200}}},
    }


def add_editor_routes(app: web.Application, server: FlowPilotServer) -> None:
    manager: GraphManager = server.manager

    def authed(handler: Handler) -> Handler:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            if not server.authorized(request, bearer_token(request)):
                return _error(401, "invalid token")
            return await handler(request)

        return wrapped

    def store() -> GraphStore:
        if manager.store is None:
            raise web.HTTPNotImplemented(text="this runtime has no graph store")
        return manager.store

    def graph_id_of(request: web.Request) -> str:
        graph_id = request.match_info["graph_id"]
        if not GRAPH_ID_RE.match(graph_id):
            raise web.HTTPBadRequest(text=f"invalid graph id {graph_id!r}")
        return graph_id

    async def catalog(request: web.Request) -> web.Response:
        return web.json_response(manager.catalog())

    async def options(request: web.Request) -> web.Response:
        max_age = 0.0 if request.query.get("refresh") else OPTIONS_CACHE_S
        return web.json_response(await server.host_options(max_age_s=max_age))

    async def node_options(request: web.Request) -> web.Response:
        body = await request.json()
        node_cls = manager.registry.get(str(body.get("type")))
        provider = getattr(node_cls, "config_options", None)
        config = body.get("config") if isinstance(body.get("config"), dict) else {}
        if provider is None:
            return web.json_response({"options": []})
        try:
            async with asyncio.timeout(NODE_OPTIONS_TIMEOUT_S):
                options = await provider(str(body.get("field")), config)
        except Exception as err:  # noqa: BLE001 - a plugin's lookup must not break the editor
            _LOGGER.warning("%s could not list %s: %s", body.get("type"), body.get("field"), err)
            return web.json_response({"options": [], "error": str(err) or type(err).__name__})
        return web.json_response(
            {
                "options": [
                    {"id": o.id, "name": o.name, "description": o.description} for o in options
                ]
            }
        )

    async def list_graphs(request: web.Request) -> web.Response:
        st = store()
        ids = sorted(set(st.ids()) | {p.name.removesuffix(".draft.json")
                                      for p in st.directory.glob("*.draft.json")})  # fmt: skip
        graphs = []
        for graph_id in ids:
            deployed = st.load(graph_id)
            draft = st.load(graph_id, draft=True)
            current = draft if draft is not None else deployed
            graphs.append(
                {
                    "id": graph_id,
                    "name": (current or {}).get("name", graph_id),
                    "deployed": graph_id in manager.deployments,
                    "has_draft": draft is not None,
                    "default": graph_id == server.default_graph,
                }
            )
        return web.json_response({"graphs": graphs})

    async def create_graph(request: web.Request) -> web.Response:
        st = store()
        body = await request.json()
        graph_id = str(body.get("id", ""))
        if not GRAPH_ID_RE.match(graph_id):
            return _error(400, "id must match [a-z0-9][a-z0-9_-]*")
        if st.load(graph_id) is not None or st.load(graph_id, draft=True) is not None:
            return _error(409, f"graph {graph_id!r} already exists")
        data = new_graph(graph_id, str(body.get("name") or graph_id))
        st.save(graph_id, data, draft=True)
        return web.json_response(
            {
                "id": graph_id,
                "draft": data,
                "issues": manager.issues(data),
                "warnings": await server.reference_warnings(data),
            }
        )

    async def get_graph(request: web.Request) -> web.Response:
        st, graph_id = store(), graph_id_of(request)
        deployed, draft = st.load(graph_id), st.load(graph_id, draft=True)
        if deployed is None and draft is None:
            return _error(404, f"no graph {graph_id!r}")
        current = draft if draft is not None else deployed
        return web.json_response(
            {
                "id": graph_id,
                "deployed": deployed,
                "draft": draft,
                "live": graph_id in manager.deployments,
                "issues": manager.issues(current),
                "warnings": await server.reference_warnings(current),
            }
        )

    async def save_draft(request: web.Request) -> web.Response:
        st, graph_id = store(), graph_id_of(request)
        data = await request.json()
        st.save(graph_id, data, draft=True)
        return web.json_response(
            {"issues": manager.issues(data), "warnings": await server.reference_warnings(data)}
        )

    async def discard_draft(request: web.Request) -> web.Response:
        store().delete_draft(graph_id_of(request))
        return web.json_response({"ok": True})

    async def deploy(request: web.Request) -> web.Response:
        st, graph_id = store(), graph_id_of(request)
        draft = st.load(graph_id, draft=True)
        if draft is None:
            return _error(409, "nothing to deploy: no draft")
        try:
            await manager.deploy_data(graph_id, draft)
        except GraphError as err:
            return _error(422, "the draft is invalid", issues=issue_dicts(list(err.issues), draft))
        except DeployError as err:
            return _error(500, f"the graph failed to start: {err}")
        return web.json_response({"deployed": True})

    routes = [
        ("GET", f"{PREFIX}/catalog", catalog),
        ("GET", f"{PREFIX}/options", options),
        ("POST", f"{PREFIX}/node-options", node_options),
        ("GET", f"{PREFIX}/graphs", list_graphs),
        ("POST", f"{PREFIX}/graphs", create_graph),
        ("GET", f"{PREFIX}/graphs/{{graph_id}}", get_graph),
        ("PUT", f"{PREFIX}/graphs/{{graph_id}}/draft", save_draft),
        ("DELETE", f"{PREFIX}/graphs/{{graph_id}}/draft", discard_draft),
        ("POST", f"{PREFIX}/graphs/{{graph_id}}/deploy", deploy),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, authed(handler))
