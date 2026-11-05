"""Shared Wyoming helpers: service address, connections, capability discovery."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info

from flowpilot_sdk import ConfigOption

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 5.0
OPTIONS_TIMEOUT_S = 4.0  # describe for the editor's lists


class WyomingError(Exception):
    """The Wyoming service failed or answered with an error event."""


@dataclass(frozen=True, slots=True)
class Address:
    host: str
    port: int

    @classmethod
    def parse(cls, uri: str) -> Address:
        parsed = urlparse(uri)
        if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port:
            raise ValueError(f"expected tcp://host:port, got {uri!r}")
        return cls(parsed.hostname, parsed.port)


@contextlib.asynccontextmanager
async def connect(address: Address, timeout_s: float) -> AsyncIterator[AsyncTcpClient]:
    client = AsyncTcpClient(address.host, address.port)
    async with asyncio.timeout(timeout_s):
        await client.connect()
    try:
        yield client
    finally:
        # Closing a connection the service already reset must not mask the real error.
        with contextlib.suppress(OSError):
            await client.disconnect()


def raise_on_error(event: Event | None) -> Event:
    """Return the event, raising if the connection closed or the service reported an error."""
    if event is None:
        raise WyomingError("connection closed by the service")
    if Error.is_type(event.type):
        err = Error.from_event(event)
        raise WyomingError(f"{err.code or 'error'}: {err.text}")
    return event


async def explain_send_failure(client: AsyncTcpClient, send_error: OSError) -> WyomingError:
    """A write failed because the service hung up; prefer the error event it sent before that."""
    try:
        async with asyncio.timeout(1.0):
            while True:
                event = raise_on_error(await client.read_event())
                _LOGGER.debug("Ignoring %s while looking for the service error", event.type)
    except WyomingError as service_error:
        return service_error
    except OSError, TimeoutError:
        return WyomingError(f"service closed the connection: {send_error}")


async def describe(address: Address, timeout_s: float, *, quiet: bool = False) -> Info | None:
    """Ask the service what it offers; ``None`` if it is unreachable."""
    try:
        async with connect(address, timeout_s) as client:
            await client.write_event(Describe().event())
            async with asyncio.timeout(timeout_s):
                while True:
                    event = raise_on_error(await client.read_event())
                    if Info.is_type(event.type):
                        return Info.from_event(event)
    except (OSError, TimeoutError, WyomingError) as err:
        log = _LOGGER.debug if quiet else _LOGGER.warning
        log("Wyoming service %s:%s did not describe itself: %s", *_hp(address), err)
        return None


async def describe_config(config: Mapping[str, Any]) -> Info | None:
    """``describe`` the service a node's (possibly unfinished) config points to, for editors.

    ``None`` while there is no valid address yet; raises when the service does not answer, so the
    editor can say why the list is empty.
    """
    try:
        address = Address.parse(str(config.get("uri") or ""))
    except ValueError:
        return None
    info = await describe(address, OPTIONS_TIMEOUT_S, quiet=True)
    if info is None:
        raise WyomingError(f"{address.host}:{address.port} does not answer")
    return info


def languages_option(codes: Iterable[str]) -> list[ConfigOption]:
    """Language codes as options; editors show the language names (``format: language``)."""
    return [ConfigOption(code) for code in sorted(set(codes))]


def _hp(address: Address) -> tuple[str, int]:
    return address.host, address.port
