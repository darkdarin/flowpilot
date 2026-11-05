"""Config flow: the FlowPilot add-on found through Supervisor discovery."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component

from .conftest import TOKEN, Runtime

pytestmark = [
    pytest.mark.usefixtures("socket_enabled"),  # the flow talks to a real runtime
    # heartbeats of the runtime's own websockets (aiohttp TestServer in the same loop), not HA's
    pytest.mark.parametrize("expected_lingering_timers", [True]),
]


@pytest.fixture(autouse=True)
async def core(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


def discovery(runtime: Runtime, token: str = TOKEN) -> HassioServiceInfo:
    url = urlparse(runtime.url)
    return HassioServiceInfo(
        config={"host": url.hostname, "port": url.port, "token": token, "addon": "FlowPilot"},
        name="FlowPilot",
        slug="local_flowpilot",
        uuid="1234",
    )


async def test_addon_discovery_creates_entry(hass: HomeAssistant, echo_runtime: Runtime) -> None:
    result = await hass.config_entries.flow.async_init(
        "flowpilot",
        context={"source": config_entries.SOURCE_HASSIO},
        data=discovery(echo_runtime),
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FlowPilot"
    assert result["data"] == {"url": echo_runtime.url, "token": TOKEN}
    await hass.async_block_till_done()

    # the add-on announces itself again (e.g. after a restart with a new token): entry updated
    entry = hass.config_entries.async_entries("flowpilot")[0]
    result = await hass.config_entries.flow.async_init(
        "flowpilot",
        context={"source": config_entries.SOURCE_HASSIO},
        data=discovery(echo_runtime, token="rotated"),
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data["token"] == "rotated"
    await hass.async_block_till_done()  # the updated entry reloads


async def test_addon_discovery_with_a_wrong_token(
    hass: HomeAssistant, echo_runtime: Runtime
) -> None:
    result = await hass.config_entries.flow.async_init(
        "flowpilot",
        context={"source": config_entries.SOURCE_HASSIO},
        data=discovery(echo_runtime, token="wrong"),
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
