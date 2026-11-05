"""Home Assistant Supervisor discovery: tell Home Assistant where the add-on's runtime is.

The FlowPilot integration receives ``{host, port, token}`` and offers to set itself up, so the user
does not type a URL or copy the token. See the integration's ``async_step_hassio``.
"""

from __future__ import annotations

import logging
import os
import socket

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
SERVICE = "flowpilot"


async def announce(port: int, token: str, *, url: str = SUPERVISOR_URL) -> bool:
    """Publish the discovery message; ``False`` (logged) when the Supervisor refuses it."""
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        return False
    host = socket.gethostname()  # the add-on's name on the Supervisor's internal network
    payload = {"service": SERVICE, "config": {"host": host, "port": port, "token": token}}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{url}/discovery",
                json=payload,
                headers={"Authorization": f"Bearer {supervisor_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            if resp.status >= 400:
                _LOGGER.warning(
                    "Supervisor refused discovery: %s %s", resp.status, await resp.text()
                )
                return False
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("cannot reach the Supervisor for discovery: %s", err)
        return False
    _LOGGER.info("announced to Home Assistant as %s:%s", host, port)
    return True
