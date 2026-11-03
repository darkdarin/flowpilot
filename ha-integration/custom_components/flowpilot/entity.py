"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def service_device(entry: ConfigEntry) -> DeviceInfo:
    """One service device per runtime connection, so entity ids read ``*.flowpilot_*``."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FlowPilot",
        manufacturer="FlowPilot",
        entry_type=DeviceEntryType.SERVICE,
    )
