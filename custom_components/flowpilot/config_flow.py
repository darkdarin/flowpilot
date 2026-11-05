"""Config flow: address and token of the FlowPilot runtime, typed in or discovered.

The FlowPilot app announces itself through Supervisor discovery (``{host, port, token}``); the
user then only confirms. An app that comes back with a new address or token updates its entry.
Adding the integration by hand on Home Assistant OS also uses that announcement when it exists, so
the address and token form is only for runtimes outside the Supervisor (Docker and the like).
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .client import FlowPilotClient, FlowPilotError
from .const import CONF_FALLBACK_TTS, CONF_TOKEN, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL, default="http://127.0.0.1:8765"): str,
        probatio.Required(CONF_TOKEN): str,
    }
)


async def async_find_app(hass: HomeAssistant) -> tuple[str, dict[str, Any], str] | None:
    """The FlowPilot app's announcement held by the Supervisor: (app slug, config, name)."""
    if not is_hassio(hass):
        return None
    from homeassistant.components.hassio import SupervisorError, get_supervisor_client

    try:
        messages = await get_supervisor_client(hass).discovery.list()
    except SupervisorError as err:
        _LOGGER.debug("No Supervisor discovery: %s", err)
        return None
    for message in messages:
        if message.service == DOMAIN:
            return message.addon, dict(message.config), "FlowPilot"
    return None


class FlowPilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect Home Assistant to a FlowPilot runtime."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FlowPilotOptionsFlow:
        return FlowPilotOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None and (found := await async_find_app(self.hass)) is not None:
            return await self._discovered_app(*found)
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
        """The FlowPilot app announced its runtime."""
        return await self._discovered_app(
            discovery_info.slug, discovery_info.config, discovery_info.name
        )

    async def _discovered_app(
        self, slug: str, config: dict[str, Any], name: str
    ) -> ConfigFlowResult:
        url = f"http://{config['host']}:{config['port']}"
        await self.async_set_unique_id(f"hassio_{slug}")
        self._abort_if_unique_id_configured(updates={CONF_URL: url, CONF_TOKEN: config["token"]})
        self._async_abort_entries_match({CONF_URL: url})
        self._discovered = {
            CONF_URL: url,
            CONF_TOKEN: config["token"],
            "addon": config.get("addon") or name,
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


class FlowPilotOptionsFlow(OptionsFlow):
    """The fallback voice: speaks what FlowPilot has no speech for (see tts.py)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {k: v for k, v in user_input.items() if v}
            return self.async_create_entry(data=options)
        ours = [
            e.entity_id
            for e in er.async_entries_for_config_entry(
                er.async_get(self.hass), self.config_entry.entry_id
            )
            if e.domain == "tts"
        ]
        schema = probatio.Schema(
            {
                probatio.Optional(
                    CONF_FALLBACK_TTS,
                    description={
                        "suggested_value": self.config_entry.options.get(CONF_FALLBACK_TTS)
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="tts", exclude_entities=ours)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
