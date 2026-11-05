"""FlowPilot: run Assist turns through a FlowPilot graph."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .client import FlowPilotClient
from .const import CONF_TOKEN, CONF_URL, SIGNAL_CONNECTION
from .host import build_host_ops
from .references import ReferenceMonitor

PLATFORMS = [Platform.CONVERSATION, Platform.STT]

type FlowPilotConfigEntry = ConfigEntry[FlowPilotClient]


async def async_setup_entry(hass: HomeAssistant, entry: FlowPilotConfigEntry) -> bool:
    client = FlowPilotClient(
        hass,
        entry.data[CONF_URL],
        entry.data[CONF_TOKEN],
        signal=SIGNAL_CONNECTION.format(entry_id=entry.entry_id),
    )
    client.host_ops.update(build_host_ops(hass, entry))
    monitor = ReferenceMonitor(hass, entry.entry_id)
    client.on_references = monitor.update
    monitor.start()
    entry.async_on_unload(monitor.stop)
    entry.runtime_data = client
    client.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FlowPilotConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded
