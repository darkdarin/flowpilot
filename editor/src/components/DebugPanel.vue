<script setup lang="ts">
// Live debug drawer: the recent turns on the left; on the right the selected turn in tabs: its
// data (what went through each node's inputs and outputs), its journal and its timeline.
import { onBeforeUnmount, ref, watch } from "vue";
import { t } from "../i18n";
import { stopPlaying } from "../playback";
import { currentTurn, debug } from "../store";
import { fmtMs } from "../timing";
import DataView from "./DataView.vue";
import JournalPanel from "./JournalPanel.vue";
import TimingView from "./TimingView.vue";

const TABS = ["data", "journal", "timeline"] as const;
type Tab = (typeof TABS)[number];

// the tab stays as it was, per browser
const saved = localStorage.getItem("flowpilot.debugTab") as Tab | null;
const tab = ref<Tab>(saved && TABS.includes(saved) ? saved : "timeline");
watch(tab, (value) => localStorage.setItem("flowpilot.debugTab", value));

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
        <span class="ms">{{ turn.durationMs !== undefined ? fmtMs(turn.durationMs) : "…" }}</span>
      </button>
    </div>

    <div v-if="currentTurn" class="results">
      <div class="tabs" role="tablist">
        <button
          v-for="name in TABS"
          :key="name"
          role="tab"
          class="tab"
          :class="{ on: tab === name }"
          :aria-selected="tab === name"
          :title="t(`debugTab.${name}Hint`)"
          @click="tab = name"
        >
          {{ t(`debugTab.${name}`) }}
        </button>
      </div>
      <div class="content" :class="tab">
        <DataView v-if="tab === 'data'" />
        <JournalPanel v-else-if="tab === 'journal'" />
        <TimingView v-else />
      </div>
    </div>
  </section>
</template>

<style scoped>
.debug {
  display: grid;
  grid-template-columns: var(--left-width, 250px) minmax(0, 1fr);
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

.ms {
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--muted);
}

.results {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.tabs {
  display: flex;
  gap: 2px;
  padding: 6px 12px 0;
  border-bottom: 1px solid var(--line);
}

.tab {
  padding: 5px 12px 6px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--muted);
  font-size: 12.5px;
  cursor: pointer;
}

.tab:hover {
  color: var(--text);
}

.tab.on {
  color: var(--text);
  border-bottom-color: var(--focus);
  font-weight: 600;
}

.content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* the Data tab scrolls inside its own columns */
.content.data {
  overflow: hidden;
}
</style>
