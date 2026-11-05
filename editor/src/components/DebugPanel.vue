<script setup lang="ts">
// Live debug drawer: recent turns and a waterfall of node runs for the selected turn.
import { computed, onBeforeUnmount } from "vue";
import { t } from "../i18n";
import { download, play, playing, removeRecording, stopPlaying } from "../playback";
import { currentTurn, debug, nodes, selectedId, specs } from "../store";
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

const span = computed(() => {
  const turn = currentTurn.value;
  if (!turn) return 1;
  const ends = Object.values(turn.nodes).map((r) => r.startMs + (r.durationMs ?? 0));
  return Math.max(turn.durationMs ?? 0, ...ends, 1);
});

function ms(value: number) {
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`;
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
  <section class="debug" :aria-label="t('debug.title')">
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
      <div class="axis">
        <span>0</span>
        <span>{{ ms(span / 2) }}</span>
        <span>{{ ms(span) }}</span>
      </div>
      <button
        v-for="row in rows"
        :key="row.id"
        class="row"
        :class="{ sel: selectedId === row.id }"
        @click="selectedId = row.id"
      >
        <code class="nid">{{ row.id }}</code>
        <span class="track">
          <span
            class="bar"
            :class="`s-${row.run.status}`"
            :style="{
              left: pct(row.run.startMs),
              width: row.run.durationMs !== undefined ? `max(3px, ${pct(row.run.durationMs)})` : `calc(100% - ${pct(row.run.startMs)})`,
              '--cat': categoryColor(row.category),
            }"
            :title="row.run.error ?? ''"
          />
          <span
            v-for="call in currentTurn.hostCalls.filter((c) => c.node === row.id)"
            :key="call.op + call.t"
            class="host"
            :style="{ left: pct(call.t) }"
            :title="call.op"
          />
        </span>
        <span class="dur">{{ row.run.durationMs !== undefined ? ms(row.run.durationMs) : "…" }}</span>
      </button>
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

.axis {
  display: flex;
  justify-content: space-between;
  margin-left: 130px;
  margin-right: 70px;
  color: var(--faint);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  border-bottom: 1px solid var(--line);
  padding-bottom: 3px;
  margin-bottom: 4px;
}

.row {
  display: grid;
  grid-template-columns: 130px 1fr 70px;
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
