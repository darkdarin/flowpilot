"""Repair issues for deployed FlowPilot graphs that refer to Home Assistant things that are gone.

The runtime sends what its deployed graphs use (``references`` message). This monitor checks those
references after Home Assistant has started, whenever the entity registry or loaded components
change, and periodically; a graph with missing references gets one repair issue, which disappears
once the references resolve again (the entity is back or the graph no longer uses it).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components import conversation, stt, tts
from homeassistant.const import EVENT_COMPONENT_LOADED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .const import (
    DOMAIN,
    OPTION_CONVERSATION,
    OPTION_STT,
    OPTION_TTS,
    REFERENCE_CHECK_DELAY_S,
    REFERENCE_CHECK_INTERVAL_S,
)

ISSUE_PREFIX = "missing_references_"
COMPONENTS = {OPTION_STT: "stt", OPTION_TTS: "tts", OPTION_CONVERSATION: "conversation"}


def reference_exists(hass: HomeAssistant, kind: str, ref_id: str) -> bool | None:
    """Whether Home Assistant can serve the reference; ``None`` for kinds it does not own."""
    component = {OPTION_STT: "stt", OPTION_TTS: "tts", OPTION_CONVERSATION: "conversation"}.get(
        kind
    )
    if component is None:
        return None  # e.g. Wyoming services: FlowPilot connects to them directly
    if component not in hass.config.components:
        return False  # the helpers below need their component; without it nothing resolves
    if kind == OPTION_STT:
        return stt.async_get_speech_to_text_entity(hass, ref_id) is not None
    if kind == OPTION_TTS:
        return tts.async_resolve_engine(hass, ref_id) is not None
    return conversation.async_get_agent_info(hass, ref_id) is not None


class ReferenceMonitor:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._entry_id = entry_id
        self._graphs: dict[str, list[dict[str, Any]]] | None = None
        self._started = False
        self._pending: CALLBACK_TYPE | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._issues: set[str] = set()

    @callback
    def start(self) -> None:
        self._unsubs += [
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_event),
            self.hass.bus.async_listen(EVENT_COMPONENT_LOADED, self._on_event),
            async_track_time_interval(
                self.hass, self._on_timer, timedelta(seconds=REFERENCE_CHECK_INTERVAL_S)
            ),
            async_at_started(self.hass, self._on_started),
        ]

    @callback
    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._pending is not None:
            self._pending()
            self._pending = None
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues.clear()

    @callback
    def update(self, graphs: dict[str, list[dict[str, Any]]]) -> None:
        """References of the deployed graphs, as reported by the runtime."""
        self._graphs = graphs
        self._schedule(0)

    @callback
    def _on_started(self, hass: HomeAssistant) -> None:
        # Before startup other integrations may not have set up their entities yet.
        self._started = True
        self._schedule(0)

    @callback
    def _on_event(self, event: Event[Any]) -> None:
        self._schedule(REFERENCE_CHECK_DELAY_S)

    @callback
    def _on_timer(self, now: Any) -> None:
        self._schedule(0)

    @callback
    def _schedule(self, delay_s: float) -> None:
        if self._pending is not None:
            self._pending()
        self._pending = async_call_later(self.hass, delay_s, self._run)

    @callback
    def _run(self, now: Any) -> None:
        self._pending = None
        self.check()

    @callback
    def check(self) -> None:
        if not self._started or self._graphs is None:
            return
        wanted: set[str] = set()
        for graph_id, refs in self._graphs.items():
            missing = [
                ref
                for ref in refs
                if reference_exists(self.hass, str(ref.get("kind")), str(ref.get("id"))) is False
            ]
            if not missing:
                continue
            issue_id = f"{ISSUE_PREFIX}{self._entry_id}_{graph_id}"
            wanted.add(issue_id)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_references",
                translation_placeholders={
                    "graph": graph_id,
                    "references": "\n".join(
                        f"- {ref['id']} (node {ref['node']}, {ref['field']})" for ref in missing
                    ),
                },
            )
        for issue_id in self._issues - wanted:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = wanted
