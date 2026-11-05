"""Config flow: the FlowPilot add-on found through Supervisor discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from aiohasupervisor.models import Discovery
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component

from custom_components.flowpilot.config_flow import async_find_app

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


async def test_adding_by_hand_uses_the_announced_app(
    hass: HomeAssistant, echo_runtime: Runtime
) -> None:
    """On Home Assistant OS "Add integration" must not ask for the address and token."""
    url = urlparse(echo_runtime.url)
    found = (
        "988b5cb9_flowpilot",
        {"host": url.hostname, "port": url.port, "token": TOKEN},
        "FlowPilot",
    )
    with patch("custom_components.flowpilot.config_flow.async_find_app", return_value=found):
        result = await hass.config_entries.flow.async_init(
            "flowpilot", context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"url": echo_runtime.url, "token": TOKEN}
    assert result["result"].unique_id == "hassio_988b5cb9_flowpilot"
    await hass.async_block_till_done()


async def test_adding_by_hand_without_the_app_asks_for_the_address(hass: HomeAssistant) -> None:
    with patch("custom_components.flowpilot.config_flow.async_find_app", return_value=None):
        result = await hass.config_entries.flow.async_init(
            "flowpilot", context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"


async def test_find_app_reads_the_supervisor_announcement(hass: HomeAssistant) -> None:
    assert await async_find_app(hass) is None  # not Home Assistant OS

    other = Discovery(addon="core_mosquitto", service="mqtt", uuid=uuid4(), config={})
    ours = Discovery(
        addon="988b5cb9_flowpilot",
        service="flowpilot",
        uuid=uuid4(),
        config={"host": "988b5cb9-flowpilot", "port": 8765, "token": "t"},
    )
    client = MagicMock()
    client.discovery.list = AsyncMock(return_value=[other, ours])
    with (
        patch("custom_components.flowpilot.config_flow.is_hassio", return_value=True),
        patch("homeassistant.components.hassio.get_supervisor_client", return_value=client),
    ):
        assert await async_find_app(hass) == (
            "988b5cb9_flowpilot",
            {"host": "988b5cb9-flowpilot", "port": 8765, "token": "t"},
            "FlowPilot",
        )
        client.discovery.list = AsyncMock(return_value=[other])
        assert await async_find_app(hass) is None
