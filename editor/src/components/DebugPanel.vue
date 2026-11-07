<script setup lang="ts">
// Live debug drawer: recent turns and a waterfall of node runs for the selected turn.
import { computed, onBeforeUnmount } from "vue";
import { t } from "../i18n";
import { download, play, playing, removeRecording, stopPlaying } from "../playback";
import { currentTurn, debug, edges, nodes, selectedId, specs } from "../store";
import { currentTimings as timings, fmtMs, turnTiming } from "../timing";
import { categoryColor } from "../types";

const rows = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return [];
  return Object.entries(turn.nodes)
    .map(([id, run]) => {
      const type = nodes.value.find((n) => n.id === id)?.data?.type ?? "";
      return { id, run, category: specs.value.get(type)?.category ?? "logic" };
    })
    .sort((a, b) => a.run.startMs - b.run.startMs);
});


/** The turn's answer delays: from the end of speech (or the typed text) to text and sound. */
const headline = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return null;
  const tt = turnTiming(turn, nodes.value, edges.value);
  if (tt.from === undefined || (tt.firstText === undefined && tt.firstSound === undefined)) return null;
  const after = (v: number | undefined) => (v === undefined ? undefined : fmtMs(Math.max(0, v - tt.from!)));
  return {
    label: t(tt.speechEnd !== undefined ? "debug.speechToText" : "debug.textToText"),
    text: after(tt.firstText),
    sound: after(tt.firstSound),
  };
});

function nodeTitle(id: string, run: { durationMs?: number; error?: string }): string {
  const tm = timings.value[id] ?? {};
  const at = (v: number | undefined) => (v === undefined ? "—" : fmtMs(v));
  const details = t("debug.nodeTiming", {
    inFirst: at(tm.inFirst),
    outFirst: at(tm.outFirst),
    inEnd: at(tm.inEnd),
    outEnd: at(tm.outEnd),
    run: at(run.durationMs),
  });
  return run.error ? `${run.error}\n${details}` : details;
}

const span = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return 1;
  const ends = Object.values(turn.nodes).map((r) => r.startMs + (r.durationMs ?? 0));
  return Math.max(turn.durationMs ?? 0, ...ends, 1);
});

const ms = fmtMs;

/** The selected node's journal in this turn; the column shows when any node wrote one. */
const hasJournal = computed(() => Object.keys(currentTurn.value?.logs ?? {}).length > 0);
const journal = computed(() =>
  selectedId.value ? (currentTurn.value?.logs[selectedId.value] ?? []) : null,
);

function kindLabel(kind: string): string {
  const key = `log.${kind}` as Parameters<typeof t>[0];
  const text = t(key);
  return text === key ? kind : text;
}

function entryClass(entry: { kind: string; attrs: Record<string, unknown> }): string {
  if (entry.attrs.ok === false || entry.kind.startsWith("llm.no_")) return "bad";
  return entry.kind.split(".")[0];
}

/** The node's solid bar: while its output flowed (first output → end), else its whole run. */
function barStyle(row: { id: string; category: string; run: { startMs: number; durationMs?: number; status: string } }) {
  const tm = timings.value[row.id] ?? {};
  const failed = !["ok", "running"].includes(row.run.status);
  const start = !failed && tm.outFirst !== undefined ? tm.outFirst : row.run.startMs;
  const runEnd = row.run.durationMs !== undefined ? row.run.startMs + row.run.durationMs : undefined;
  const end = !failed && tm.outEnd !== undefined ? tm.outEnd : runEnd;
  return {
    left: pct(start),
    width: end !== undefined ? `max(3px, ${pct(Math.max(0, end - start))})` : `calc(100% - ${pct(start)})`,
    "--cat": categoryColor(row.category),
  };
}

function pct(ms: number) {
  return `${(ms / span.value) * 100}%`;
}

onBeforeUnmount(stopPlaying);

function status(s: string) {
  const key = `status.${s}` as Parameters<typeof t>[0];
  return t(key);
}
</script>

<template>
  <section class="debug" :class="{ 'with-journal': hasJournal }" :aria-label="t('debug.title')">
    <div class="turns">
      <p v-if="!debug.connected" class="note warn">{{ t("debug.offline") }}</p>
      <p v-if="debug.redeployed" class="note">{{ t("debug.redeployed") }}</p>
      <p v-if="!debug.turns.length" class="note">{{ t("debug.waiting") }}</p>
      <button
        v-for="turn in debug.turns"
        :key="turn.id"
        class="turn"
        :class="[`s-${turn.status}`, { active: currentTurn?.id === turn.id }]"
        @click="debug.selectedTurn = turn.id"
      >
        <span class="dot" />
        <code>{{ turn.id.slice(-6) }}</code>
        <span class="st">{{ status(turn.status) }}</span>
        <span class="ms">{{ turn.durationMs !== undefined ? ms(turn.durationMs) : "…" }}</span>
      </button>
    </div>

    <div v-if="currentTurn" class="waterfall">
      <div v-if="currentTurn.recordings.length" class="recordings">
        <span class="label">{{ t("debug.recordings") }}</span>
        <span v-for="rec in currentTurn.recordings" :key="rec.file" class="recording">
          <button
            class="play"
            :class="{ on: playing === rec.file }"
            :title="playing === rec.file ? t('debug.stopPlaying') : t('debug.play')"
            :aria-label="playing === rec.file ? t('debug.stopPlaying') : t('debug.play')"
            @click="play(rec.file)"
          >
            <svg v-if="playing === rec.file" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
            <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" /></svg>
          </button>
          <code>{{ rec.node }}</code>
          <span class="secs">{{ rec.seconds.toFixed(1) }} s</span>
          <button class="icon" :title="t('debug.download')" :aria-label="t('debug.download')" @click="download(rec.file)">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10" /></svg>
          </button>
          <button class="icon" :title="t('debug.deleteRecording')" :aria-label="t('debug.deleteRecording')" @click="removeRecording(rec.file)">
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8.5h4.8l.6-8.5" /></svg>
          </button>
        </span>
      </div>
      <p v-if="headline" class="headline">
        {{ headline.label }}
        <b v-if="headline.text">{{ headline.text }}</b>
        <template v-if="headline.sound"> · {{ t("debug.toSound") }} <b>{{ headline.sound }}</b></template>
      </p>
      <div class="head">
        <span />
        <div class="axis">
          <span>0</span>
          <span>{{ ms(span / 2) }}</span>
          <span>{{ ms(span) }}</span>
        </div>
        <span class="col" :title="t('debug.reactionHint')">{{ t("debug.reaction") }}</span>
        <span class="col" :title="t('debug.tailHint')">{{ t("debug.tail") }}</span>
      </div>
      <button
        v-for="row in rows"
        :key="row.id"
        class="row"
        :class="{ sel: selectedId === row.id }"
        @click="selectedId = row.id"
      >
        <code class="nid">{{ row.id }}</code>
        <span class="track" :title="nodeTitle(row.id, row.run)">
          <span
            class="alive"
            :style="{
              left: pct(row.run.startMs),
              width: row.run.durationMs !== undefined ? `max(2px, ${pct(row.run.durationMs)})` : `calc(100% - ${pct(row.run.startMs)})`,
            }"
          />
          <span
            v-if="timings[row.id]?.inFirst !== undefined && timings[row.id]?.outFirst !== undefined"
            class="wait"
            :style="{
              left: pct(timings[row.id].inFirst!),
              width: pct(Math.max(0, timings[row.id].outFirst! - timings[row.id].inFirst!)),
              '--cat': categoryColor(row.category),
            }"
          />
          <span
            class="bar"
            :class="`s-${row.run.status}`"
            :style="barStyle(row)"
          />
          <span
            v-if="timings[row.id]?.inEnd !== undefined"
            class="in-end"
            :style="{ left: pct(timings[row.id].inEnd!) }"
          />
          <span
            v-for="call in currentTurn.hostCalls.filter((c) => c.node === row.id)"
            :key="call.op + call.t"
            class="host"
            :style="{ left: pct(call.t) }"
            :title="call.op"
          />
        </span>
        <span class="dur" :title="t('debug.reactionHint')">{{ timings[row.id]?.reaction !== undefined ? ms(timings[row.id].reaction!) : "" }}</span>
        <span class="dur" :title="t('debug.tailHint')">{{ timings[row.id]?.tail !== undefined ? ms(timings[row.id].tail!) : "" }}</span>
      </button>
    </div>

    <div v-if="hasJournal" class="journal">
      <h3>
        {{ t("debug.journal") }}
        <code v-if="selectedId" class="jnode">{{ selectedId }}</code>
      </h3>
      <p v-if="journal === null" class="note">{{ t("debug.journalPick") }}</p>
      <p v-else-if="!journal.length" class="note">{{ t("debug.journalEmpty") }}</p>
      <ol v-else class="entries">
        <li v-for="(entry, i) in journal" :key="i" class="entry" :class="entryClass(entry)">
          <details :open="entry.kind === 'tool' && entry.attrs.ok === false">
            <summary>
              <span class="at">{{ ms(entry.t) }}</span>
              <span class="kind">{{ kindLabel(entry.kind) }}</span>
              <span class="text">{{ entry.summary }}</span>
              <span v-if="entry.attrs.ms !== undefined" class="took">{{ ms(Number(entry.attrs.ms)) }}</span>
            </summary>
            <pre v-if="entry.detail">{{ entry.detail }}</pre>
          </details>
        </li>
      </ol>
    </div>
  </section>
</template>

<style scoped>
.debug {
  display: grid;
  grid-template-columns: 230px 1fr;
  height: 100%;
  min-height: 0;
  background: var(--panel);
  border-top: 1px solid var(--line);
}

.debug.with-journal {
  grid-template-columns: 230px minmax(0, 1fr) minmax(300px, 34%);
}

.journal {
  overflow-y: auto;
  border-left: 1px solid var(--line);
  padding: 8px 12px 12px;
  min-width: 0;
}

.journal h3 {
  margin: 2px 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}

.jnode {
  margin-left: 6px;
  color: var(--text);
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
  grid-template-columns: 58px auto minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 8px;
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

.entry .at,
.entry .took {
  color: var(--faint);
  font-variant-numeric: tabular-nums;
  font-size: 11px;
}

.entry .kind {
  padding: 0 6px;
  border-radius: 8px;
  font-size: 11px;
  white-space: nowrap;
  background: color-mix(in srgb, var(--kind, var(--muted)) 18%, transparent);
  color: var(--kind, var(--muted));
}

.entry .text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.entry details[open] .text {
  white-space: normal;
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

.entry pre {
  margin: 4px 0 6px 66px;
  padding: 8px 10px;
  max-height: 320px;
  overflow: auto;
  border-radius: 6px;
  background: var(--panel-raised);
  border: 1px solid var(--line);
  font-size: 11.5px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.turns {
  overflow-y: auto;
  border-right: 1px solid var(--line);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.note {
  margin: 4px 6px 8px;
  color: var(--muted);
  font-size: 12px;
}

.note.warn {
  color: var(--warn);
}

.turn {
  display: grid;
  grid-template-columns: 10px auto 1fr auto;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.turn:hover,
.turn.active {
  background: var(--panel-raised);
}

.turn.active {
  box-shadow: inset 2px 0 0 var(--focus);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--faint);
}

.s-ok .dot {
  background: var(--ok);
}

.s-degraded .dot {
  background: var(--warn);
}

.s-timeout .dot {
  background: var(--danger);
}

.s-running .dot {
  background: var(--warn);
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.3;
  }
}

.st {
  color: var(--muted);
  font-size: 12px;
}

.ms,
.dur {
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--muted);
}

.waterfall {
  overflow-y: auto;
  padding: 8px 16px 12px;
}

.recordings {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-bottom: 8px;
}

.recordings .label {
  color: var(--muted);
  font-size: 12px;
}

.recording {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 6px 2px 2px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--panel-raised);
}

.recording button {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.recording button:hover {
  color: var(--text);
  background: var(--line);
}

.recording .play {
  background: color-mix(in srgb, var(--cat-debug) 22%, transparent);
  color: var(--text);
}

.recording .play.on {
  background: var(--cat-debug);
  color: var(--primary-text);
}

.recording svg {
  width: 13px;
  height: 13px;
}

.recording .play svg {
  fill: currentColor;
}

.recording .icon svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.recording .secs {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.headline {
  margin: 0 0 8px 6px;
  font-size: 12.5px;
  color: var(--muted);
}

.headline b {
  color: var(--text);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.head {
  display: grid;
  grid-template-columns: 130px 1fr 78px 78px;
  align-items: end;
  border-bottom: 1px solid var(--line);
  padding-bottom: 3px;
  margin-bottom: 4px;
  color: var(--faint);
  font-size: 11px;
}

.axis {
  display: flex;
  justify-content: space-between;
  font-variant-numeric: tabular-nums;
}

.col {
  text-align: right;
  padding-right: 6px;
  cursor: help;
}

.row {
  display: grid;
  grid-template-columns: 130px 1fr 78px 78px;
  align-items: center;
  width: 100%;
  height: 24px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.row:hover,
.row.sel {
  background: var(--panel-raised);
}

.nid {
  overflow: hidden;
  text-overflow: ellipsis;
  padding-left: 6px;
}

.dur {
  text-align: right;
  padding-right: 6px;
}

.track {
  position: relative;
  height: 12px;
}

.alive {
  position: absolute;
  top: 3px;
  height: 6px;
  border-radius: 3px;
  background: var(--line);
}

/* from the first input to the first output: the node's reaction */
.wait {
  position: absolute;
  top: 5px;
  height: 2px;
  background: var(--cat);
  opacity: 0.6;
}

/* the input ended here; the bar after it is what the node still did */
.in-end {
  position: absolute;
  top: -1px;
  width: 0;
  height: 14px;
  border-left: 1.5px dashed var(--muted);
}

.bar {
  position: absolute;
  top: 0;
  height: 12px;
  border-radius: 3px;
  background: var(--cat);
  opacity: 0.85;
}

.bar.s-running {
  opacity: 0.5;
  background: repeating-linear-gradient(45deg, var(--cat) 0 6px, transparent 6px 10px);
}

/* hatched, so a failed run stands apart from the red of debug nodes */
.bar.s-error,
.bar.s-timeout,
.bar.s-cancelled {
  opacity: 1;
  background: repeating-linear-gradient(-45deg, var(--danger) 0 4px, color-mix(in srgb, var(--danger) 45%, transparent) 4px 8px);
}

.host {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 18px;
  background: var(--text);
  border-radius: 1px;
}
</style>
