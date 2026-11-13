<script setup lang="ts">
// Live debug drawer: recent turns and a waterfall of node runs for the selected turn.
import { computed, onBeforeUnmount, ref } from "vue";
import { t } from "../i18n";
import { stopPlaying } from "../playback";
import { currentTurn, debug, nodes, selectedId, specs } from "../store";
import { currentTurnTiming, describeMoment, fmtAt, fmtMs, sinkId, currentTimings as timings } from "../timing";
import { categoryColor } from "../types";
import JournalPanel from "./JournalPanel.vue";

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


/** The turn's times: the whole turn, to the first sound and text (and after the end of speech). */
const headline = computed(() => {
  const tt = currentTurnTiming.value;
  if (!tt) return null;
  return {
    total: tt.total !== undefined ? fmtMs(tt.total) : undefined,
    sound: tt.firstSound !== undefined ? fmtMs(tt.firstSound) : undefined,
    text: tt.firstText !== undefined ? fmtMs(tt.firstText) : undefined,
    afterSpeech:
      tt.speechEnd !== undefined && tt.effective !== undefined
        ? fmtMs(Math.max(0, tt.effective - tt.speechEnd))
        : undefined,
  };
});

/** The hover: a line across the timeline at one moment, and what the node under it did then. */
const rowsEl = ref<HTMLElement | null>(null);
const cursor = ref<{ x: number; at: number; row?: string; top: number; flip: boolean } | null>(null);

function onMove(ev: MouseEvent) {
  const box = rowsEl.value;
  const track = box?.querySelector<HTMLElement>(".track");
  if (!box || !track) return;
  const tr = track.getBoundingClientRect();
  const br = box.getBoundingClientRect();
  if (ev.clientX < tr.left || ev.clientX > tr.right) {
    cursor.value = null;
    return;
  }
  const row = (ev.target as HTMLElement).closest<HTMLElement>(".row");
  cursor.value = {
    x: ev.clientX - br.left,
    at: ((ev.clientX - tr.left) / tr.width) * span.value,
    row: row?.dataset.id,
    top: row ? row.offsetTop : ev.clientY - br.top,
    flip: ev.clientX - tr.left > tr.width * 0.6,
  };
}

const hover = computed(() => {
  const c = cursor.value;
  if (!c) return null;
  return { ...c, lines: c.row ? describeMoment(c.row, c.at) : [] };
});

const span = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return 1;
  const ends = Object.values(turn.nodes).map((r) => r.startMs + (r.durationMs ?? 0));
  return Math.max(turn.durationMs ?? 0, ...ends, 1);
});

const ms = fmtMs;

/** The journal column shows once the turn has something in it (live debug only). */
const hasJournal = computed(() => {
  const turn = currentTurn.value;
  return Boolean(turn && (turn.outputs.length || Object.keys(turn.logs).length || Object.keys(turn.notes).length));
});

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

/** The part of the bar after every input ended: the node still passes data on, on its own time. */
function tailStyle(row: { id: string; category: string; run: { status: string } }) {
  const tm = timings.value[row.id];
  if (!tm || ["error", "timeout", "cancelled"].includes(row.run.status)) return null;
  if (tm.inEnd === undefined || tm.outFirst === undefined || tm.outEnd === undefined) return null;
  const start = Math.max(tm.inEnd, tm.outFirst);
  if (tm.outEnd - start < span.value / 400) return null; // too short to see
  return { left: pct(start), width: pct(tm.outEnd - start), "--cat": categoryColor(row.category) };
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
      <p v-if="headline" class="headline" :title="t('debug.effectiveHint')">
        <template v-if="headline.sound">{{ t("debug.toFirstSound") }} <b>{{ headline.sound }}</b></template>
        <template v-if="headline.afterSpeech"> ({{ t("debug.afterSpeech") }} <b>{{ headline.afterSpeech }}</b>)</template>
        <template v-if="headline.text"> · {{ t("debug.toFirstText") }} <b>{{ headline.text }}</b></template>
        <template v-if="headline.total"> · {{ t("debug.total") }} <b>{{ headline.total }}</b></template>
      </p>
      <div class="head">
        <span />
        <div class="axis">
          <span>0</span>
          <span>{{ ms(span / 2) }}</span>
          <span>{{ ms(span) }}</span>
        </div>
        <span class="col" :title="t('debug.reactionHint')">{{ t("debug.reaction") }}</span>
        <span class="col" :title="t('debug.delayHint')">{{ t("debug.delay") }}</span>
      </div>
      <div ref="rowsEl" class="rows" @mousemove="onMove" @mouseleave="cursor = null">
      <button
        v-for="row in rows"
        :key="row.id"
        class="row"
        :class="{ sel: selectedId === row.id, 'on-path': timings[row.id]?.onPath }"
        :data-id="row.id"
        @click="selectedId = row.id"
      >
        <code class="nid">{{ row.id }}</code>
        <span class="track">
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
          <span v-if="tailStyle(row)" class="tail" :style="tailStyle(row)!" />
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
        <span v-if="row.id === sinkId" class="dur total" :title="t('debug.effectiveHint')">{{ currentTurnTiming?.effective !== undefined ? `▶ ${ms(currentTurnTiming.effective)}` : "" }}</span>
        <span v-else class="dur" :title="timings[row.id]?.onPath ? t('debug.onPath') : t('debug.delayHint')">{{ timings[row.id]?.delay !== undefined ? `+${ms(timings[row.id].delay!)}` : "" }}</span>
      </button>
      <template v-if="hover">
        <span class="cursor" :style="{ left: `${hover.x}px` }" />
        <div
          class="tip"
          :class="{ flip: hover.flip, below: hover.top < 60 }"
          :style="{ left: `${hover.x}px`, top: `${hover.top}px` }"
        >
          <b class="tip-at">{{ t("debug.at", { at: fmtAt(hover.at) }) }}</b>
          <template v-if="hover.row">
            <code class="tip-node">{{ hover.row }}</code>
            <span v-for="(line, i) in hover.lines" :key="i" class="tip-line">{{ line }}</span>
          </template>
        </div>
      </template>
      </div>
    </div>

    <JournalPanel v-if="hasJournal" />
  </section>
</template>

<style scoped>
.debug {
  display: grid;
  grid-template-columns: var(--left-width, 250px) 1fr;
  height: 100%;
  min-height: 0;
  background: var(--panel);
  border-top: 1px solid var(--line);
}

.debug.with-journal {
  grid-template-columns: var(--left-width, 250px) minmax(0, 1fr) minmax(300px, 34%);
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

/* the path to the answer's first sound: its nodes' delays add up to the time to it */
.row.on-path .nid {
  color: var(--focus);
  font-weight: 600;
}

.row.on-path .bar {
  box-shadow: 0 0 0 1.5px var(--focus);
}

.row.on-path .dur:last-child,
.dur.total {
  color: var(--text);
  font-weight: 600;
}

/* after every input ended: the node still passes data on, on its own time */
.tail {
  position: absolute;
  top: 0;
  height: 12px;
  border-radius: 0 3px 3px 0;
  background: repeating-linear-gradient(-45deg, color-mix(in srgb, var(--panel) 55%, transparent) 0 3px, transparent 3px 7px);
  pointer-events: none;
}

/* the hover: one moment across every node, like a chart's crosshair */
.rows {
  position: relative;
}

.cursor {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1px solid var(--focus);
  pointer-events: none;
}

.tip {
  position: absolute;
  z-index: 2;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 420px;
  padding: 6px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  box-shadow: var(--shadow);
  font-size: 11.5px;
  line-height: 1.35;
  pointer-events: none;
  transform: translate(10px, calc(-100% - 4px));
}

.tip.flip {
  transform: translate(calc(-100% - 10px), calc(-100% - 4px));
}

.tip.below {
  transform: translate(10px, 28px);
}

.tip.flip.below {
  transform: translate(calc(-100% - 10px), 28px);
}

.tip-at {
  font-variant-numeric: tabular-nums;
}

.tip-node {
  color: var(--muted);
}

.tip-line {
  white-space: normal;
}
</style>
