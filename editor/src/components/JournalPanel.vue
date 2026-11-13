<script setup lang="ts">
// The turn's journal in live debug: one list for the whole turn, in time order. What each node
// passed on (text, data, audio to play), what the nodes wrote (model requests, tools, quick
// commands) and what they reported (timings, levels). A node filter on top follows the node
// selected on the canvas or the timeline.
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { download, play, playing } from "../playback";
import { currentTurn, selectedId, type LogEntry, type OutputData } from "../store";
import { fmtMs } from "../timing";
import { typeColor, typeShort } from "../types";
import JsonBlock from "./JsonBlock.vue";

type Entry =
  | { kind: "log"; node: string; t: number; log: LogEntry }
  | { kind: "out"; node: string; t: number; out: OutputData }
  | { kind: "note"; node: string; t: number; event: string; attrs: Record<string, unknown> };

const entries = computed<Entry[]>(() => {
  const turn = currentTurn.value;
  if (!turn) return [];
  const all: Entry[] = [
    ...Object.entries(turn.logs).flatMap(([node, logs]) =>
      logs.map((log): Entry => ({ kind: "log", node, t: log.t, log })),
    ),
    ...turn.outputs.map((out): Entry => ({ kind: "out", node: out.node, t: out.t, out })),
    ...Object.entries(turn.notes).flatMap(([node, notes]) =>
      notes.map((n): Entry => ({ kind: "note", node, t: n.t, event: n.event, attrs: n.attrs })),
    ),
  ];
  return all.sort((a, b) => a.t - b.t);
});

/** The nodes that wrote or passed on something, in the order they first did. */
const nodeIds = computed(() => [...new Set(entries.value.map((e) => e.node))]);

// "" shows every node; selecting a node on the canvas or the timeline filters to it
const filter = ref("");
watch(selectedId, (id) => (filter.value = id ?? ""), { immediate: true });

const shown = computed(() =>
  filter.value ? entries.value.filter((e) => e.node === filter.value) : entries.value,
);

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

function isAudio(out: OutputData) {
  return out.type === "audio.pcm/1";
}
</script>

<template>
  <div class="journal">
    <header>
      <h3>{{ t("debug.journal") }}</h3>
      <select v-model="filter" class="filter" :aria-label="t('journal.filter')">
        <option value="">{{ t("journal.all") }}</option>
        <option v-for="id in nodeIds" :key="id" :value="id">{{ id }}</option>
        <option v-if="filter && !nodeIds.includes(filter)" :value="filter">{{ filter }}</option>
      </select>
    </header>
    <p v-if="!shown.length" class="note">{{ filter ? t("journal.nodeEmpty") : t("journal.empty") }}</p>
    <ol v-else class="entries">
      <li v-for="(entry, i) in shown" :key="i" class="entry" :class="entry.kind === 'log' ? logClass(entry.log) : entry.kind">
        <!-- what a node passed on -->
        <details v-if="entry.kind === 'out'" :open="!entry.out.same">
          <summary>
            <span class="at">{{ fmtMs(entry.t) }}</span>
            <button v-if="!filter" class="node" @click.stop.prevent="pick(entry.node)">{{ entry.node }}</button><span v-else />
            <span class="kind">→ {{ entry.out.port }}<code v-if="typeShort(entry.out.type) !== entry.out.port" class="type" :style="{ color: typeColor(entry.out.type) }">{{ typeShort(entry.out.type) }}</code></span>
            <span class="text" :class="{ preview: !entry.out.same }">
              <template v-if="entry.out.same">{{ t("journal.same", { from: entry.out.same }) }}</template>
              <template v-else-if="isAudio(entry.out) && entry.out.seconds !== undefined">{{ entry.out.seconds.toFixed(1) }} s</template>
              <template v-else-if="entry.out.text !== undefined">{{ entry.out.text }}</template>
            </span>
            <span v-if="entry.out.items > 1" class="took">{{ t("journal.items", { n: entry.out.items }) }}</span>
          </summary>
          <div class="body">
            <p v-if="entry.out.error" class="err">{{ entry.out.error }}</p>
            <div v-if="isAudio(entry.out) && entry.out.file" class="audio">
              <button
                class="play"
                :class="{ on: playing === entry.out.file }"
                :title="playing === entry.out.file ? t('debug.stopPlaying') : t('debug.play')"
                :aria-label="playing === entry.out.file ? t('debug.stopPlaying') : t('debug.play')"
                @click="play(entry.out.file!)"
              >
                <svg v-if="playing === entry.out.file" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
                <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" /></svg>
              </button>
              <span class="secs">{{ entry.out.seconds?.toFixed(1) }} s</span>
              <button class="icon" :title="t('debug.download')" :aria-label="t('debug.download')" @click="download(entry.out.file!)">
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10" /></svg>
              </button>
            </div>
            <template v-else-if="entry.out.text !== undefined">
              <pre class="plain">{{ entry.out.text || "—" }}</pre>
              <p v-if="entry.out.partials" class="hint">{{ t("journal.partials", { n: entry.out.partials }) }}</p>
            </template>
            <JsonBlock v-else-if="entry.out.json !== undefined" :text="entry.out.json" />
            <p v-if="entry.out.more" class="hint">{{ t("journal.more", { n: entry.out.more }) }}</p>
          </div>
        </details>

        <!-- what a node wrote: model requests, tools, quick commands -->
        <details v-else-if="entry.kind === 'log'" :open="entry.log.kind === 'tool' && entry.log.attrs.ok === false">
          <summary>
            <span class="at">{{ fmtMs(entry.t) }}</span>
            <button v-if="!filter" class="node" @click.stop.prevent="pick(entry.node)">{{ entry.node }}</button><span v-else />
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
            <button v-if="!filter" class="node" @click.stop.prevent="pick(entry.node)">{{ entry.node }}</button><span v-else />
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
  overflow-y: auto;
  border-left: 1px solid var(--line);
  padding: 0 12px 12px;
  min-width: 0;
}

header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0 8px;
  background: var(--panel);
}

h3 {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}

.filter {
  margin-left: auto;
  max-width: 60%;
  padding: 2px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  color: var(--text);
  font-size: 12px;
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
  grid-template-columns: 52px auto auto minmax(0, 1fr) auto;
  /* without the node filter each entry names its node; with it the column stays, empty */
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

.type {
  margin-left: 5px;
  font-size: 10.5px;
}

/* the summary shows the beginning of the data; open, the body shows all of it */
details[open] > summary .preview {
  visibility: hidden;
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

.entry.out {
  --kind: var(--cat-speech, var(--ok));
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

.plain {
  margin: 0;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

.hint {
  margin: 3px 0 0;
  font-size: 11px;
  color: var(--faint);
}

.err {
  margin: 0 0 4px;
  font-size: 11.5px;
  color: var(--danger);
}

.audio {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px 3px 3px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--panel-raised);
}

.audio button {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.audio button:hover {
  color: var(--text);
  background: var(--line);
}

.audio .play {
  background: color-mix(in srgb, var(--cat-speech, var(--ok)) 25%, transparent);
  color: var(--text);
}

.audio .play.on {
  background: var(--cat-speech, var(--ok));
  color: var(--primary-text);
}

.audio svg {
  width: 13px;
  height: 13px;
}

.audio .play svg {
  fill: currentColor;
}

.audio .icon svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.secs {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
