"""REST API for the graph editor: node catalog, graphs, drafts, validation and deploy.

All routes need ``Authorization: Bearer <token>``.

- ``GET  /api/editor/catalog``                node types (ports, config schemas) and payload types
- ``GET  /api/editor/options``                what connected hosts offer for marked settings
                                              (``?refresh=1`` skips the short cache)
- ``POST /api/editor/node-options``           ``{type, field, config}``: choices a node lists itself
                                              for a setting (``NODE_OPTIONS_KEY``)
- ``GET  /api/editor/secrets``                secret names: set or not, and the graphs using them
                                              (values are never returned)
- ``PUT  /api/editor/secrets/{name}``         ``{value}``: set a secret; graphs using it redeploy.
                                              ``DELETE`` removes it
- ``GET  /api/editor/memory``                 facts of the long-term memory and the people (Home
                                              Assistant users) they may belong to
- ``POST /api/editor/memory``                 ``{text, owner}``: add a fact (``owner`` null: the
                                              home's); ``PUT``/``DELETE /api/editor/memory/{id}``
- ``GET  /api/editor/commands``               quick commands people taught (and the people);
                                              ``PUT /api/editor/commands/{id}`` ``{phrases,
                                              reply}``, ``DELETE`` removes one
- ``GET  /api/editor/skills``                 skills described for the LLM agent; ``POST``
                                              creates one, ``PUT``/``DELETE
                                              /api/editor/skills/{id}``
- ``GET  /api/editor/recordings``             debug recordings (``?graph=``), newest first
- ``GET  /api/editor/recordings/{file}``      one recording as audio/wav; ``DELETE`` removes it
- ``PUT  /api/editor/default``                ``{graph}``: the graph turns go to when a client
                                              names none (Assist); it must be deployed
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
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from flowpilot_core import GraphError

from .auth import bearer_token
from .manager import GRAPH_ID_RE, DeployError, GraphManager, GraphStore, issue_dicts
from .probe import list_recordings, recording_path, recordings_dir
from .references import OPTIONS_CACHE_S
from .secret_store import secret_fields

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
        "schema_version": 2,
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
        # A node lists choices with its own credentials (a cloud API's models): secret references
        # arrive resolved, as at start. The values stay inside the runtime.
        config = {
            k: manager.secrets.get(v["secret"], v)
            if isinstance(v, dict) and set(v) == {"secret"} and isinstance(v["secret"], str)
            else v
            for k, v in config.items()
        }
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

    def recordings() -> Path:
        if manager.data_dir is None:
            raise web.HTTPNotImplemented(text="this runtime keeps no recordings")
        return recordings_dir(manager.data_dir)

    async def list_recordings_(request: web.Request) -> web.Response:
        found = await asyncio.to_thread(list_recordings, recordings(), request.query.get("graph"))
        return web.json_response({"recordings": [r.as_dict() for r in found]})

    async def get_recording(request: web.Request) -> web.StreamResponse:
        path = recording_path(recordings(), request.match_info["file"])
        if path is None:
            return _error(404, "no such recording")
        return web.FileResponse(path, headers={"Content-Type": "audio/wav"})

    async def delete_recording(request: web.Request) -> web.Response:
        path = recording_path(recordings(), request.match_info["file"])
        if path is None:
            return _error(404, "no such recording")
        await asyncio.to_thread(path.unlink, missing_ok=True)
        return web.json_response({"ok": True})

    async def set_default(request: web.Request) -> web.Response:
        graph_id = str((await request.json()).get("graph", ""))
        try:
            server.set_default_graph(graph_id)
        except KeyError:
            return _error(409, f"graph {graph_id!r} is not deployed")
        return web.json_response({"default": graph_id})

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

    def secret_store() -> MutableMapping[str, str]:
        if not isinstance(manager.secrets, MutableMapping):
            raise web.HTTPNotImplemented(text="this runtime's secrets are read-only")
        return manager.secrets

    async def list_secrets(request: web.Request) -> web.Response:
        used: dict[str, set[str]] = {}
        if manager.store is not None:
            for graph_id in manager.store.ids():
                for draft in (False, True):
                    for *_, name in secret_fields(manager.store.load(graph_id, draft=draft)):
                        used.setdefault(name, set()).add(graph_id)
        names = sorted(set(manager.secrets) | set(used))
        return web.json_response(
            {
                "secrets": [
                    {"name": n, "set": n in manager.secrets, "graphs": sorted(used.get(n, ()))}
                    for n in names
                ]
            }
        )

    async def set_secret(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await request.json()
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, str):
            return _error(400, "send {value}")
        try:
            secret_store()[name] = value
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response({"redeployed": await manager.secret_changed(name)})

    async def delete_secret(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        secret_store().pop(name, None)
        return web.json_response({"redeployed": await manager.secret_changed(name)})

    async def list_memory(request: web.Request) -> web.Response:
        hosts = await server.host_options()
        people = hosts["options"].get("ha.user", []) if hosts["available"] else []
        return web.json_response(
            {
                "facts": [asdict(f) for f in manager.memory.all()],
                "people": people,
                "people_known": bool(hosts["available"]),
            }
        )

    def fact_body(body: Any) -> tuple[str, str | None]:
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ValueError("send {text, owner}")
        owner = body.get("owner")
        if owner is not None and not isinstance(owner, str):
            raise ValueError("owner must be a user id or null")
        return body["text"], owner

    async def add_fact(request: web.Request) -> web.Response:
        try:
            text, owner = fact_body(await request.json())
            fact = manager.memory.add(text, owner, source="editor")
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response(asdict(fact))

    async def update_fact(request: web.Request) -> web.Response:
        try:
            text, owner = fact_body(await request.json())
            fact = manager.memory.update(request.match_info["fact_id"], text=text, owner=owner)
        except KeyError:
            return _error(404, "no such fact")
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response(asdict(fact))

    async def delete_fact(request: web.Request) -> web.Response:
        if not manager.memory.remove(request.match_info["fact_id"]):
            return _error(404, "no such fact")
        return web.json_response({"removed": True})

    async def list_commands(request: web.Request) -> web.Response:
        hosts = await server.host_options()
        people = hosts["options"].get("ha.user", []) if hosts["available"] else []
        return web.json_response(
            {"commands": [asdict(c) for c in manager.commands.all()], "people": people}
        )

    async def update_command(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("phrases"), list):
            return _error(400, "send {phrases, reply}")
        try:
            command = manager.commands.update(
                request.match_info["command_id"],
                phrases=[str(p) for p in body["phrases"]],
                reply=str(body.get("reply") or ""),
            )
        except KeyError:
            return _error(404, "no such command")
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response(asdict(command))

    async def delete_command(request: web.Request) -> web.Response:
        if not manager.commands.remove(request.match_info["command_id"]):
            return _error(404, "no such command")
        return web.json_response({"removed": True})

    async def list_skills(request: web.Request) -> web.Response:
        return web.json_response({"skills": [asdict(s) for s in manager.skills.all()]})

    async def add_skill(request: web.Request) -> web.Response:
        try:
            skill = manager.skills.add(await request.json())
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response(asdict(skill))

    async def update_skill(request: web.Request) -> web.Response:
        try:
            skill = manager.skills.update(request.match_info["skill_id"], await request.json())
        except KeyError:
            return _error(404, "no such skill")
        except ValueError as err:
            return _error(400, str(err))
        return web.json_response(asdict(skill))

    async def delete_skill(request: web.Request) -> web.Response:
        if not manager.skills.remove(request.match_info["skill_id"]):
            return _error(404, "no such skill")
        return web.json_response({"removed": True})

    routes = [
        ("GET", f"{PREFIX}/skills", list_skills),
        ("POST", f"{PREFIX}/skills", add_skill),
        ("PUT", f"{PREFIX}/skills/{{skill_id}}", update_skill),
        ("DELETE", f"{PREFIX}/skills/{{skill_id}}", delete_skill),
        ("GET", f"{PREFIX}/commands", list_commands),
        ("PUT", f"{PREFIX}/commands/{{command_id}}", update_command),
        ("DELETE", f"{PREFIX}/commands/{{command_id}}", delete_command),
        ("GET", f"{PREFIX}/memory", list_memory),
        ("POST", f"{PREFIX}/memory", add_fact),
        ("PUT", f"{PREFIX}/memory/{{fact_id}}", update_fact),
        ("DELETE", f"{PREFIX}/memory/{{fact_id}}", delete_fact),
        ("GET", f"{PREFIX}/secrets", list_secrets),
        ("PUT", f"{PREFIX}/secrets/{{name}}", set_secret),
        ("DELETE", f"{PREFIX}/secrets/{{name}}", delete_secret),
        ("GET", f"{PREFIX}/catalog", catalog),
        ("GET", f"{PREFIX}/options", options),
        ("POST", f"{PREFIX}/node-options", node_options),
        ("GET", f"{PREFIX}/recordings", list_recordings_),
        ("GET", f"{PREFIX}/recordings/{{file}}", get_recording),
        ("DELETE", f"{PREFIX}/recordings/{{file}}", delete_recording),
        ("GET", f"{PREFIX}/graphs", list_graphs),
        ("PUT", f"{PREFIX}/default", set_default),
        ("POST", f"{PREFIX}/graphs", create_graph),
        ("GET", f"{PREFIX}/graphs/{{graph_id}}", get_graph),
        ("PUT", f"{PREFIX}/graphs/{{graph_id}}/draft", save_draft),
        ("DELETE", f"{PREFIX}/graphs/{{graph_id}}/draft", discard_draft),
        ("POST", f"{PREFIX}/graphs/{{graph_id}}/deploy", deploy),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, authed(handler))
