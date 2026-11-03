"""Config flow: address and token of the FlowPilot runtime."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .client import FlowPilotClient, FlowPilotError
from .const import CONF_TOKEN, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL, default="http://127.0.0.1:8765"): str,
        probatio.Required(CONF_TOKEN): str,
    }
)


class FlowPilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to a FlowPilot runtime."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            self._async_abort_entries_match({CONF_URL: url})
            client = FlowPilotClient(self.hass, url, user_input[CONF_TOKEN], signal="")
            try:
                info = await client.async_validate()
            except FlowPilotError:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError, TimeoutError, OSError:
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.debug("FlowPilot handshake: %s", info)
                return self.async_create_entry(
                    title=f"FlowPilot ({url})",
                    data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                )
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
