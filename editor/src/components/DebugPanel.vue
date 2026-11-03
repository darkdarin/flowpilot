<script setup lang="ts">
// Live debug drawer: recent turns and a waterfall of node runs for the selected turn.
import { computed } from "vue";
import { t } from "../i18n";
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

.bar.s-error,
.bar.s-timeout,
.bar.s-cancelled {
  background: var(--danger);
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
