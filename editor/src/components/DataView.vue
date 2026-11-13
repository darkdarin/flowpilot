<script setup lang="ts">
// The Data tab of live debug: the turn's nodes, the inputs and outputs of the selected one, and what
// went through the selected port. An input shows what the output wired to it passed on.
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { currentTurn, edges, nodes, selectedId, specs, type OutputData } from "../store";
import { nodeTitle } from "../texts";
import { typeColor, typeShort } from "../types";
import OutputValue from "./OutputValue.vue";
import SoundIcon from "./SoundIcon.vue";

interface Port {
  side: "in" | "out";
  name: string;
  type: string;
  /** an input: the output wired to it ("node.port") */
  from?: string;
  data?: OutputData;
}

const dataOf = (node: string, port: string) =>
  currentTurn.value?.outputs.find((o) => o.node === node && o.port === port);

/** The turn's nodes in the order they started, with how many of their ports carried something. */
const nodeList = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return [];
  return Object.entries(turn.nodes)
    .sort(([, a], [, b]) => a.startMs - b.startMs)
    .map(([id]) => {
      const type = String(nodes.value.find((n) => n.id === id)?.data?.type ?? "");
      return { id, title: nodeTitle(specs.value.get(type), type), out: turn.outputs.filter((o) => o.node === id).length };
    });
});

const ports = computed<Port[]>(() => {
  const id = selectedId.value;
  if (!id) return [];
  const type = String(nodes.value.find((n) => n.id === id)?.data?.type ?? "");
  const spec = specs.value.get(type);
  if (!spec) return [];
  const inputs = spec.inputs.map((p): Port => {
    const wire = edges.value.find((e) => e.target === id && e.targetHandle === p.name);
    const from = wire ? `${wire.source}.${wire.sourceHandle}` : undefined;
    const data = wire ? dataOf(wire.source, wire.sourceHandle ?? "") : undefined;
    return { side: "in", name: p.name, type: data?.type ?? p.type, from, data };
  });
  const outputs = spec.outputs.map((p): Port => {
    const data = dataOf(id, p.name);
    return { side: "out", name: p.name, type: data?.type ?? p.type, data };
  });
  return [...inputs, ...outputs];
});

// the selected port; a new node starts on its first output that carried something
const picked = ref<{ side: string; name: string } | null>(null);
watch(
  [selectedId, () => currentTurn.value?.id],
  () => {
    const first = ports.value.find((p) => p.side === "out" && p.data) ?? ports.value.find((p) => p.data);
    picked.value = first ? { side: first.side, name: first.name } : null;
  },
  { immediate: true },
);
const port = computed(() =>
  picked.value ? ports.value.find((p) => p.side === picked.value!.side && p.name === picked.value!.name) : undefined,
);

function summary(p: Port): string {
  if (p.side === "in" && !p.from) return t("data.notWired");
  if (!p.data) return t("data.nothing");
  if (p.data.same) return t("journal.same", { from: p.data.same });
  if (p.data.seconds !== undefined) return `${p.data.seconds.toFixed(1)} s`;
  if (p.data.text !== undefined) return p.data.text || "—";
  return p.data.items > 1 ? t("journal.items", { n: p.data.items }) : "{…}";
}
</script>

<template>
  <div class="data">
    <ul class="nodes">
      <li v-for="n in nodeList" :key="n.id">
        <button :class="{ sel: selectedId === n.id }" @click="selectedId = n.id">
          <code>{{ n.id }}</code>
          <span class="title">{{ n.title }}</span>
          <span v-if="n.out" class="count">{{ n.out }}</span>
        </button>
      </li>
    </ul>

    <div class="ports">
      <p v-if="!selectedId" class="note">{{ t("data.pickNode") }}</p>
      <template v-else>
        <template v-for="side in ['in', 'out'] as const" :key="side">
          <h4 v-if="ports.some((p) => p.side === side)">{{ side === "in" ? t("data.inputs") : t("data.outputs") }}</h4>
          <button
            v-for="p in ports.filter((x) => x.side === side)"
            :key="side + p.name"
            class="port"
            :class="{ sel: port === p, empty: !p.data }"
            @click="picked = { side: p.side, name: p.name }"
          >
            <span class="name">{{ side === "in" ? "←" : "→" }} {{ p.name }}</span>
            <code class="type" :style="{ color: typeColor(p.type) }">{{ typeShort(p.type) !== p.name ? typeShort(p.type) : "" }}</code>
            <span class="sum"><SoundIcon v-if="p.type === 'audio.pcm/1'" /> {{ summary(p) }}</span>
          </button>
        </template>
      </template>
    </div>

    <div class="value">
      <template v-if="port">
        <p class="where">
          <code>{{ selectedId }}.{{ port.name }}</code>
          <template v-if="port.from"> ← <code>{{ port.from }}</code></template>
          <span v-if="port.data && port.data.items > 1" class="items">{{ t("journal.items", { n: port.data.items }) }}</span>
        </p>
        <OutputValue v-if="port.data" :out="port.data" />
        <p v-else class="note">{{ port.side === "in" && !port.from ? t("data.notWired") : t("data.nothing") }}</p>
      </template>
      <p v-else-if="selectedId" class="note">{{ t("data.pickPort") }}</p>
    </div>
  </div>
</template>

<style scoped>
.data {
  display: grid;
  grid-template-columns: minmax(150px, 22%) minmax(200px, 30%) minmax(0, 1fr);
  height: 100%;
  min-height: 0;
}

.nodes,
.ports,
.value {
  overflow-y: auto;
  min-height: 0;
}

.nodes {
  list-style: none;
  margin: 0;
  padding: 6px;
  border-right: 1px solid var(--line);
}

.nodes button,
.port {
  display: grid;
  align-items: baseline;
  gap: 6px;
  width: 100%;
  padding: 4px 8px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text);
  text-align: left;
  font-size: 12px;
  cursor: pointer;
}

.nodes button {
  grid-template-columns: auto minmax(0, 1fr) auto;
}

.nodes button:hover,
.nodes button.sel,
.port:hover,
.port.sel {
  background: var(--panel-raised);
}

.nodes button.sel,
.port.sel {
  box-shadow: inset 2px 0 0 var(--focus);
}

.title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-size: 11.5px;
}

.count {
  padding: 0 6px;
  border-radius: 8px;
  background: var(--line);
  color: var(--muted);
  font-size: 10.5px;
}

.ports {
  padding: 6px;
  border-right: 1px solid var(--line);
}

h4 {
  margin: 6px 8px 3px;
  font-size: 11px;
  font-weight: 600;
  color: var(--faint);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.port {
  grid-template-columns: auto auto minmax(0, 1fr);
}

.port.empty .name,
.port.empty .sum {
  color: var(--faint);
}

.type {
  font-size: 10.5px;
}

.sum {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
}

.value {
  padding: 8px 12px 12px;
}

.where {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--muted);
}

.where code {
  color: var(--text);
}

.items {
  margin-left: auto;
  font-size: 11px;
  color: var(--faint);
}

.note {
  margin: 6px 8px;
  color: var(--muted);
  font-size: 12px;
}
</style>
