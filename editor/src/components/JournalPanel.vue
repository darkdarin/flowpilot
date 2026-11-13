<script setup lang="ts">
// The turn's journal in live debug: one list for the whole turn, in time order. What the nodes
// wrote (model requests, tools, quick commands) and what they reported (timings, levels); what
// went through their inputs and outputs is in the Data tab. The filter on top takes several nodes, as tags; empty, it shows them all.
// Selecting a node on the canvas or the timeline filters to that node.
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { currentTurn, selectedId, type LogEntry } from "../store";
import { fmtMs } from "../timing";
import JsonBlock from "./JsonBlock.vue";

type Entry =
  | { kind: "log"; node: string; t: number; log: LogEntry }
  | { kind: "note"; node: string; t: number; event: string; attrs: Record<string, unknown> };

const entries = computed<Entry[]>(() => {
  const turn = currentTurn.value;
  if (!turn) return [];
  const all: Entry[] = [
    ...Object.entries(turn.logs).flatMap(([node, logs]) =>
      logs.map((log): Entry => ({ kind: "log", node, t: log.t, log })),
    ),
    ...Object.entries(turn.notes).flatMap(([node, notes]) =>
      notes.map((n): Entry => ({ kind: "note", node, t: n.t, event: n.event, attrs: n.attrs })),
    ),
  ];
  return all.sort((a, b) => a.t - b.t);
});

/** The nodes that wrote or reported something, in the order they first did. */
const nodeIds = computed(() => [...new Set(entries.value.map((e) => e.node))]);

// the nodes shown; none: all of them
const filter = ref<string[]>([]);
watch(selectedId, (id) => (filter.value = id ? [id] : []), { immediate: true });

const addable = computed(() => nodeIds.value.filter((id) => !filter.value.includes(id)));
const adding = ref("");

function add() {
  if (adding.value && !filter.value.includes(adding.value)) filter.value = [...filter.value, adding.value];
  adding.value = "";
}

function remove(id: string) {
  filter.value = filter.value.filter((f) => f !== id);
}

const shown = computed(() =>
  filter.value.length ? entries.value.filter((e) => filter.value.includes(e.node)) : entries.value,
);
const oneNode = computed(() => filter.value.length === 1);

function pick(node: string) {
  selectedId.value = node;
}

function kindLabel(kind: string): string {
  const key = `log.${kind}` as Parameters<typeof t>[0];
  const text = t(key);
  return text === key ? kind : text;
}

function logClass(log: LogEntry): string {
  if (log.attrs.ok === false || log.kind.startsWith("llm.no_")) return "bad";
  return log.kind.split(".")[0];
}
</script>

<template>
  <div class="journal">
    <div class="filter" :aria-label="t('journal.filter')">
      <span v-for="id in filter" :key="id" class="tag">
        {{ id }}
        <button :title="t('journal.removeNode')" :aria-label="t('journal.removeNode')" @click="remove(id)">×</button>
      </span>
      <select v-if="addable.length" v-model="adding" class="add" :aria-label="t('journal.addNode')" @change="add">
        <option value="">{{ filter.length ? t("journal.addNode") : t("journal.all") }}</option>
        <option v-for="id in addable" :key="id" :value="id">{{ id }}</option>
      </select>
      <button v-if="filter.length" class="clear" @click="filter = []">{{ t("journal.clear") }}</button>
    </div>
    <p v-if="!shown.length" class="note">{{ filter.length ? t("journal.nodeEmpty") : t("journal.empty") }}</p>
    <ol v-else class="entries">
      <li v-for="(entry, i) in shown" :key="i" class="entry" :class="entry.kind === 'log' ? logClass(entry.log) : entry.kind">
        <!-- what a node wrote: model requests, tools, quick commands -->
        <details v-if="entry.kind === 'log'" :open="entry.log.kind === 'tool' && entry.log.attrs.ok === false">
          <summary>
            <span class="at">{{ fmtMs(entry.t) }}</span>
            <button v-if="!oneNode" class="node" @click.stop.prevent="pick(entry.node)">{{ entry.node }}</button><span v-else />
            <span class="kind">{{ kindLabel(entry.log.kind) }}</span>
            <span class="text">{{ entry.log.summary }}</span>
            <span v-if="entry.log.attrs.ms !== undefined" class="took">{{ fmtMs(Number(entry.log.attrs.ms)) }}</span>
          </summary>
          <div v-if="entry.log.detail" class="body">
            <JsonBlock :text="entry.log.detail" />
          </div>
        </details>

        <!-- what a node reported: timings, levels, counts -->
        <details v-else>
          <summary>
            <span class="at">{{ fmtMs(entry.t) }}</span>
            <button v-if="!oneNode" class="node" @click.stop.prevent="pick(entry.node)">{{ entry.node }}</button><span v-else />
            <span class="kind">{{ entry.event }}</span>
            <span class="text attrs">
              <span v-for="(value, key) in entry.attrs" :key="key"><span class="k">{{ key }}</span> {{ value ?? "—" }}</span>
            </span>
          </summary>
          <div class="body">
            <JsonBlock :value="entry.attrs" />
          </div>
        </details>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.journal {
  padding: 0 12px 12px;
  min-width: 0;
}

.filter {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  padding: 8px 0;
  background: var(--panel);
}

.tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 3px 1px 8px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--focus) 18%, transparent);
  color: var(--text);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 11.5px;
}

.tag button {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
}

.tag button:hover {
  background: var(--line);
  color: var(--text);
}

.add {
  padding: 2px 6px;
  border: 1px dashed var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  font-size: 11.5px;
  cursor: pointer;
}

.clear {
  margin-left: auto;
  padding: 1px 8px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  font-size: 11.5px;
  cursor: pointer;
}

.clear:hover {
  color: var(--text);
  background: var(--line);
}

.note {
  margin: 4px 2px;
  color: var(--muted);
  font-size: 12px;
}

.entries {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.entry summary {
  display: grid;
  /* the node column stays, empty, when one node is shown */
  grid-template-columns: 52px auto auto minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 7px;
  padding: 4px 6px;
  border-radius: 5px;
  cursor: pointer;
  font-size: 12px;
  list-style: none;
}

.entry summary::-webkit-details-marker {
  display: none;
}

.entry summary:hover,
.entry details[open] summary {
  background: var(--panel-raised);
}

.at,
.took {
  color: var(--faint);
  font-variant-numeric: tabular-nums;
  font-size: 11px;
}

.node {
  padding: 0 5px;
  border: 0;
  border-radius: 4px;
  background: var(--line);
  color: var(--text);
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 11px;
  cursor: pointer;
}

.node:hover {
  outline: 1px solid var(--focus);
}

.kind {
  padding: 0 6px;
  border-radius: 8px;
  font-size: 11px;
  white-space: nowrap;
  background: color-mix(in srgb, var(--kind, var(--muted)) 18%, transparent);
  color: var(--kind, var(--muted));
}

.text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attrs {
  display: inline-flex;
  gap: 10px;
  color: var(--muted);
}

.attrs .k {
  color: var(--faint);
}

.entry.llm {
  --kind: var(--cat-agent, #7c5cff);
}

.entry.tool {
  --kind: var(--warn);
}

.entry.quick {
  --kind: var(--cat-io, var(--ok));
}

.entry.memory {
  --kind: var(--ok);
}

.entry.bad {
  --kind: var(--danger);
}

.body {
  margin: 4px 0 6px 59px;
}
</style>
