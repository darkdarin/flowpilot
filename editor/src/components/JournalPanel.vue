<script setup lang="ts">
// The turn's journal in live debug: one list for the whole turn, in time order. What the nodes
// wrote (model requests, tools, quick commands) and what they reported (timings, levels); what
// went through their inputs and outputs is in the Data tab. The selected entry's content shows on
// the right. The filter on top takes several nodes, as tags; empty, it shows them all. A node's
// name on an entry adds it to the filter.
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { currentTurn, type LogEntry } from "../store";
import { fmtMs } from "../timing";
import JsonBlock from "./JsonBlock.vue";

type Entry =
  | { key: string; kind: "log"; node: string; t: number; log: LogEntry }
  | { key: string; kind: "note"; node: string; t: number; event: string; attrs: Record<string, unknown> };

const entries = computed<Entry[]>(() => {
  const turn = currentTurn.value;
  if (!turn) return [];
  const all: Entry[] = [
    ...Object.entries(turn.logs).flatMap(([node, logs]) =>
      logs.map((log, i): Entry => ({ key: `log:${node}:${i}`, kind: "log", node, t: log.t, log })),
    ),
    ...Object.entries(turn.notes).flatMap(([node, notes]) =>
      notes.map((n, i): Entry => ({ key: `note:${node}:${i}`, kind: "note", node, t: n.t, event: n.event, attrs: n.attrs })),
    ),
  ];
  return all.sort((a, b) => a.t - b.t);
});

/** The nodes that wrote or reported something, in the order they first did. */
const nodeIds = computed(() => [...new Set(entries.value.map((e) => e.node))]);

// the nodes shown; none: all of them
const filter = ref<string[]>([]);

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

// the entry whose content shows on the right; another turn starts with none
const picked = ref<string | null>(null);
watch(() => currentTurn.value?.id, () => (picked.value = null));
const entry = computed(() => shown.value.find((e) => e.key === picked.value));

function pick(node: string) {
  if (!filter.value.includes(node)) filter.value = [...filter.value, node];
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
    <div class="split">
      <div class="list">
        <p v-if="!shown.length" class="note">{{ filter.length ? t("journal.nodeEmpty") : t("journal.empty") }}</p>
        <ol v-else class="entries">
          <li v-for="e in shown" :key="e.key" :class="e.kind === 'log' ? logClass(e.log) : e.kind">
            <button class="row" :class="{ sel: picked === e.key }" @click="picked = e.key">
              <span class="at">{{ fmtMs(e.t) }}</span>
              <span v-if="!oneNode" class="node" :title="t('journal.addToFilter')" @click.stop="pick(e.node)">{{ e.node }}</span><span v-else />
              <template v-if="e.kind === 'log'">
                <span class="kind">{{ kindLabel(e.log.kind) }}</span>
                <span class="text">{{ e.log.summary }}</span>
                <span v-if="e.log.attrs.ms !== undefined" class="took">{{ fmtMs(Number(e.log.attrs.ms)) }}</span>
              </template>
              <template v-else>
                <span class="kind">{{ e.event }}</span>
                <span class="text attrs">
                  <span v-for="(value, key) in e.attrs" :key="key"><span class="k">{{ key }}</span> {{ value ?? "—" }}</span>
                </span>
              </template>
            </button>
          </li>
        </ol>
      </div>

      <div class="detail">
        <template v-if="entry">
          <p class="where">
            <span class="at">{{ fmtMs(entry.t) }}</span>
            <code>{{ entry.node }}</code>
            <span class="kind" :class="entry.kind === 'log' ? logClass(entry.log) : 'note'">{{ entry.kind === "log" ? kindLabel(entry.log.kind) : entry.event }}</span>
            <span v-if="entry.kind === 'log' && entry.log.attrs.ms !== undefined" class="took">{{ fmtMs(Number(entry.log.attrs.ms)) }}</span>
          </p>
          <p v-if="entry.kind === 'log'" class="summary">{{ entry.log.summary }}</p>
          <JsonBlock v-if="entry.kind === 'log' && entry.log.detail" :text="entry.log.detail" />
          <JsonBlock v-else-if="entry.kind === 'log'" :value="entry.log.attrs" />
          <JsonBlock v-else :value="entry.attrs" />
        </template>
        <p v-else class="note">{{ t("journal.pick") }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.journal {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.split {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  min-height: 0;
}

.list,
.detail {
  overflow-y: auto;
  min-height: 0;
}

.list {
  padding: 0 8px 12px 12px;
  border-right: 1px solid var(--line);
}

.detail {
  padding: 8px 12px 12px;
}

.where {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 12px;
}

.summary {
  margin: 0 0 8px;
  font-size: 12.5px;
}

.filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
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

.row {
  display: grid;
  /* the node column stays, empty, when one node is shown */
  grid-template-columns: 52px auto auto minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 7px;
  width: 100%;
  padding: 4px 6px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text);
  text-align: left;
  font-size: 12px;
  cursor: pointer;
}

.row:hover,
.row.sel {
  background: var(--panel-raised);
}

.row.sel {
  box-shadow: inset 2px 0 0 var(--focus);
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

li.llm,
.kind.llm {
  --kind: var(--cat-agent, #7c5cff);
}

li.tool,
.kind.tool {
  --kind: var(--warn);
}

li.quick,
.kind.quick {
  --kind: var(--cat-io, var(--ok));
}

li.memory,
.kind.memory {
  --kind: var(--ok);
}

li.bad,
.kind.bad {
  --kind: var(--danger);
}

</style>
