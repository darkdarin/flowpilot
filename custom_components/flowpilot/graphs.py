"""Entities per deployed graph: each Assist assistant can pick the graph it runs.

The FlowPilot "Speech-to-text" and "Agent" entities go to the runtime's default graph; next to
them every deployed graph gets a speech-to-text and an agent entity named after it
(``stt.flowpilot_<graph id>``, ``conversation.flowpilot_<graph id>``). The
runtime reports its graphs (``graphs`` message); entities appear for new graphs, follow renames and
are removed from the registry when their graph is no longer deployed.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import FlowPilotClient


class GraphEntity(Entity):
    """An entity that sends its turns to one graph (``None``: the runtime's default graph)."""

    graph: str | None = None
    _base_name = ""  # "Speech-to-text", "Agent": the default graph's entities
    _domain = ""  # "stt", "conversation"

    def setup_graph(self, graph: str | None, graph_name: str | None) -> None:
        self.graph = graph
        self._attr_name = self._base_name if graph is None else self._graph_name(graph_name)
        if graph is not None:  # stt.flowpilot_<graph id>, not the (long, changing) graph name
            self.entity_id = f"{self._domain}.flowpilot_{graph.replace('-', '_')}"

    def _graph_name(self, graph_name: str | None) -> str:
        # the assistant's speech-to-text and agent pickers already say which is which
        return graph_name or self.graph or self._base_name

    @callback
    def rename_graph(self, graph_name: str) -> None:
        name = self._graph_name(graph_name)
        if name != self._attr_name:
            self._attr_name = name
            if self.hass is not None:
                self.async_write_ha_state()


def graph_unique_id(entry: ConfigEntry, kind: str, graph: str | None) -> str:
    """``<entry>-<kind>`` for the default graph (unchanged ids), ``<entry>-<kind>-graph-<id>``."""
    base = f"{entry.entry_id}-{kind}"
    return base if graph is None else f"{base}-graph-{graph}"


@callback
def async_track_graphs[E: GraphEntity](
    hass: HomeAssistant,
    entry: ConfigEntry[FlowPilotClient],
    async_add_entities: AddConfigEntryEntitiesCallback,
    kind: str,
    factory: Callable[[str, str], E],
) -> None:
    """Keep one entity per deployed graph for this platform."""
    client = entry.runtime_data
    added: dict[str, E] = {}
    prefix = graph_unique_id(entry, kind, "")

    @callback
    def sync() -> None:
        if not client.graphs_known:
            return  # until the runtime has told us, keep what the registry has
        new = {g: factory(g, name) for g, name in client.graphs.items() if g not in added}
        added.update(new)
        if new:
            async_add_entities(list(new.values()))
        for graph, entity in added.items():
            if graph in client.graphs:
                entity.rename_graph(client.graphs[graph])
        registry = er.async_get(hass)
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
            graph = reg.unique_id.removeprefix(prefix)
            if reg.unique_id.startswith(prefix) and graph not in client.graphs:
                registry.async_remove(reg.entity_id)
                added.pop(graph, None)

    entry.async_on_unload(async_dispatcher_connect(hass, client.graphs_signal, sync))
    sync()
