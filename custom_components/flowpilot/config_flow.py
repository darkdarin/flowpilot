"""Config flow: address and token of the FlowPilot runtime, typed in or discovered.

The FlowPilot add-on announces itself through Supervisor discovery (``{host, port, token}``); the
user then only confirms. An add-on that comes back with a new address or token updates its entry.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

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

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

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

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The FlowPilot add-on announced its runtime."""
        config = discovery_info.config
        url = f"http://{config['host']}:{config['port']}"
        await self.async_set_unique_id(f"hassio_{discovery_info.slug}")
        self._abort_if_unique_id_configured(updates={CONF_URL: url, CONF_TOKEN: config["token"]})
        self._async_abort_entries_match({CONF_URL: url})
        self._discovered = {
            CONF_URL: url,
            CONF_TOKEN: config["token"],
            "addon": config.get("addon") or discovery_info.name,
        }
        self.context["title_placeholders"] = {"addon": self._discovered["addon"]}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url, token = self._discovered[CONF_URL], self._discovered[CONF_TOKEN]
            client = FlowPilotClient(self.hass, url, token, signal="")
            try:
                await client.async_validate()
            except FlowPilotError:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError, TimeoutError, OSError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=str(self._discovered["addon"]),
                    data={CONF_URL: url, CONF_TOKEN: token},
                )
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"addon": str(self._discovered["addon"])},
            errors=errors,
        )
