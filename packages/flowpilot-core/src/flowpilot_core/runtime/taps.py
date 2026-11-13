"""What every node passed on, for live debug.

While someone watches a graph in live debug, the runtime shows each output of the turn to an
``OutputTap`` as it flows: its type and header, every payload, and how it ended. The host (the
FlowPilot server) decides what to keep: text as text, audio as a recording, other data as JSON.
Nothing is shown to a tap in a turn nobody watches, so people's words and voices stay out of it.

A tap must be quick and must not raise: it runs inside the node's own ``send``. Anything slow
(writing a file) it does in ``close``, which the runtime awaits when the turn's nodes are done.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from flowpilot_sdk import TurnContext

_LOGGER = logging.getLogger(__name__)


class OutputTap(Protocol):
    def opened(self, node_id: str, port: str, type_id: str, meta: Mapping[str, Any]) -> None: ...

    def item(self, node_id: str, port: str, payload: Any) -> None: ...

    def ended(self, node_id: str, port: str, state: str) -> None: ...

    async def close(self) -> None: ...


# Makes the tap of one watched turn.
OutputTapFactory = Callable[[TurnContext], OutputTap]


class SafeTap:
    """Calls a tap without letting it break the node that sends: a tap's bug loses debug data
    only."""

    def __init__(self, tap: OutputTap) -> None:
        self._tap = tap
        self._broken = False

    def _call(self, method: str, *args: Any) -> None:
        if self._broken:
            return
        try:
            getattr(self._tap, method)(*args)
        except Exception:
            self._broken = True
            _LOGGER.exception("output tap failed in %s; live debug loses this turn's data", method)

    def opened(self, node_id: str, port: str, type_id: str, meta: Mapping[str, Any]) -> None:
        self._call("opened", node_id, port, type_id, meta)

    def item(self, node_id: str, port: str, payload: Any) -> None:
        self._call("item", node_id, port, payload)

    def ended(self, node_id: str, port: str, state: str) -> None:
        self._call("ended", node_id, port, state)

    async def close(self) -> None:
        try:
            await self._tap.close()
        except Exception:
            _LOGGER.exception("output tap failed to close")
