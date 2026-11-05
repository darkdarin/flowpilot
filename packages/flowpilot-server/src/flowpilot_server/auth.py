"""Who may call the runtime: a bearer token, or a trusted peer (the Supervisor on the Ingress port).

Home Assistant Ingress proxies the add-on's web UI into the HA frontend after HA has authenticated
the user; requests then come from the Supervisor (172.30.32.2) on a port that is not published.
"""

from __future__ import annotations

from aiohttp import web

TRUSTED_PEERS = web.AppKey("trusted_peers", frozenset[str])


def is_trusted(request: web.Request) -> bool:
    """The request comes from a peer this app trusts (the Supervisor on the Ingress port)."""
    return request.remote is not None and request.remote in request.app.get(TRUSTED_PEERS, ())


def ingress_prefix(request: web.Request) -> str:
    """The path prefix Home Assistant Ingress serves us under (``/api/hassio_ingress/<t>``)."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/") if is_trusted(request) else ""


def bearer_token(request: web.Request) -> str:
    return request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
