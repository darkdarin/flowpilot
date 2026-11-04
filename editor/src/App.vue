<script setup lang="ts">
import { Background } from "@vue-flow/background";
import { type Connection, VueFlow, useVueFlow } from "@vue-flow/core";
import { MiniMap } from "@vue-flow/minimap";
import { computed, markRaw, nextTick, onMounted, ref } from "vue";
import DebugPanel from "./components/DebugPanel.vue";
import FpNode from "./components/FpNode.vue";
import Inspector from "./components/Inspector.vue";
import Login from "./components/Login.vue";
import Palette from "./components/Palette.vue";
import TypedEdge from "./components/TypedEdge.vue";
import { type Locale, locale, setLocale, t } from "./i18n";
import {
  addNode,
  api,
  connect,
  createGraph,
  debug,
  deploy,
  deploying,
  discardDraft,
  edges,
  flash,
  graphId,
  graphs,
  hasDraft,
  issues,
  live,
  warnings,
  makeEdge,
  nodes,
  notice,
  openGraph,
  saving,
  selectedId,
  specs,
  token,
} from "./store";
import { typeColor } from "./types";

const nodeTypes = { fp: markRaw(FpNode) };
const edgeTypes = { typed: markRaw(TypedEdge) };
const { screenToFlowCoordinate, fitView, onNodesInitialized } = useVueFlow();

const booting = ref(Boolean(token.value));
const theme = ref(document.documentElement.dataset.theme ?? "dark");
const creating = ref(false);
const newId = ref("");
const newName = ref("");
const newError = ref("");

onMounted(async () => {
  if (token.value) {
    try {
      await connect(token.value);
    } catch {
      /* fall back to the login form */
    }
    booting.value = false;
  }
});

// fit once per opened graph, after Vue Flow has measured the nodes
let fittedFor: string | null = null;
onNodesInitialized(() => {
  if (fittedFor === graphId.value) return;
  fittedFor = graphId.value;
  void nextTick(() => fitView({ padding: 0.15, maxZoom: 1 }));
});

function toggleTheme() {
  theme.value = theme.value === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme.value;
  localStorage.setItem("flowpilot.theme", theme.value);
}

function toggleLocale() {
  setLocale((locale.value === "en" ? "ru" : "en") as Locale);
}

const issueLabel = computed(() =>
  issues.value.length === 0 ? t("issues.none") : issues.value.length === 1 ? t("issues.one") : t("issues.count", { n: issues.value.length }),
);

const stateLabel = computed(() => {
  if (saving.value) return t("state.saving");
  if (hasDraft.value) return t("state.draft");
  return live.value ? t("state.live") : t("state.notDeployed");
});

// --- connections -----------------------------------------------------------------------------

function portOf(nodeId: string, port: string | null | undefined, side: "inputs" | "outputs") {
  const type = nodes.value.find((n) => n.id === nodeId)?.data?.type;
  return type ? specs.value.get(type)?.[side].find((p) => p.name === port) : undefined;
}

function isValidConnection(c: Connection): boolean {
  const out = portOf(c.source, c.sourceHandle, "outputs");
  const inp = portOf(c.target, c.targetHandle, "inputs");
  if (!out || !inp || c.source === c.target) return false;
  return out.type === inp.type;
}

function onConnect(c: Connection) {
  const out = portOf(c.source, c.sourceHandle, "outputs");
  const inp = portOf(c.target, c.targetHandle, "inputs");
  if (!out || !inp) return;
  if (out.type !== inp.type) return flash(t("error.connect"), "error");
  // an input takes one wire: a new connection replaces the old one
  const kept = edges.value.filter((e) => !(e.target === c.target && e.targetHandle === c.targetHandle));
  edges.value = [...kept, makeEdge(c.source, c.sourceHandle!, c.target, c.targetHandle!, out.type)];
}

function connectionLineColor() {
  return "var(--muted)";
}

// --- palette drop ----------------------------------------------------------------------------

function onDrop(event: DragEvent) {
  const type = event.dataTransfer?.getData("application/flowpilot-node");
  if (!type) return;
  const pos = screenToFlowCoordinate({ x: event.clientX, y: event.clientY });
  addNode(type, { x: pos.x - 115, y: pos.y - 30 });
}

function addFromPalette(type: string) {
  const pane = document.querySelector(".canvas")!.getBoundingClientRect();
  addNode(type, screenToFlowCoordinate({ x: pane.left + pane.width / 2 - 115, y: pane.top + pane.height / 2 - 40 }));
}

// --- top bar actions -------------------------------------------------------------------------

async function onDeploy() {
  if (await deploy()) flash(t("action.deployed"));
  else if (notice.value?.text === "invalid") flash(t("error.invalid"), "error");
}

async function onDiscard() {
  if (window.confirm(t("action.discardConfirm"))) await discardDraft();
}

async function onPickGraph(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (value === "__new") {
    creating.value = true;
    (event.target as HTMLSelectElement).value = graphId.value ?? "";
    return;
  }
  await openGraph(value);
}

async function onCreate() {
  newError.value = "";
  try {
    await createGraph(newId.value.trim(), newName.value.trim() || newId.value.trim());
    creating.value = false;
    newId.value = newName.value = "";
  } catch (err) {
    newError.value = (err as Error).message;
  }
}

function legendTypes() {
  return [...new Set(edges.value.map((e) => e.data?.type ?? ""))].filter(Boolean).sort();
}
</script>

<template>
  <div v-if="booting" class="boot" />
  <Login v-else-if="!api" />
  <div v-else class="shell" :class="{ debugging: debug.enabled }">
    <header class="bar">
      <div class="brand" aria-label="FlowPilot">
        <svg viewBox="0 0 28 16" aria-hidden="true">
          <path d="M5 8 C 12 8, 14 3, 23 3" />
          <path d="M5 8 C 12 8, 14 13, 23 13" />
          <circle cx="4" cy="8" r="3" />
        </svg>
        <span>FlowPilot</span>
      </div>

      <label class="graph-pick">
        <span class="sr">{{ t("graph.pick") }}</span>
        <select class="field" :value="graphId ?? ''" @change="onPickGraph">
          <option v-for="g in graphs" :key="g.id" :value="g.id">{{ g.name }} ({{ g.id }})</option>
          <option value="__new">+ {{ t("graph.new") }}</option>
        </select>
      </label>

      <span class="state" :class="{ draft: hasDraft, live: live && !hasDraft }">
        <i />{{ stateLabel }}
      </span>
      <span class="issues" :class="{ bad: issues.length }">{{ issueLabel }}</span>
      <span v-if="warnings.length" class="issues warn">{{ t("issues.warnings", { n: warnings.length }) }}</span>

      <div class="spacer" />

      <button v-if="hasDraft && live" class="btn ghost" @click="onDiscard">{{ t("action.discard") }}</button>
      <button class="btn primary" :disabled="!hasDraft || deploying || issues.length > 0" @click="onDeploy">
        {{ deploying ? t("action.deploying") : t("action.deploy") }}
      </button>

      <div class="sep" />

      <button class="btn debug-toggle" :class="{ on: debug.enabled }" :aria-pressed="debug.enabled" @click="debug.enabled = !debug.enabled">
        <i />{{ t("action.debug") }}
      </button>
      <button class="btn icon ghost" :title="t('action.fit')" :aria-label="t('action.fit')" @click="fitView({ padding: 0.2 })">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4" /></svg>
      </button>
      <button class="btn icon ghost" :title="theme === 'dark' ? t('theme.light') : t('theme.dark')" :aria-label="theme === 'dark' ? t('theme.light') : t('theme.dark')" @click="toggleTheme">
        <svg v-if="theme === 'dark'" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" /><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3" /></svg>
        <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5Z" /></svg>
      </button>
      <button class="btn icon ghost lang" :aria-label="locale === 'en' ? 'Русский' : 'English'" @click="toggleLocale">
        {{ locale === "en" ? "RU" : "EN" }}
      </button>
    </header>

    <Palette class="left" @add="addFromPalette" />

    <main class="canvas" @dragover.prevent @drop="onDrop">
      <VueFlow
        v-model:nodes="nodes"
        v-model:edges="edges"
        :node-types="nodeTypes"
        :edge-types="edgeTypes"
        :is-valid-connection="isValidConnection"
        :connection-line-style="{ stroke: connectionLineColor() }"
        :default-edge-options="{ type: 'typed' }"
        :min-zoom="0.2"
        :max-zoom="2"
        :snap-to-grid="true"
        :snap-grid="[11, 11]"
        delete-key-code="Delete"
        @connect="onConnect"
        @node-click="({ node }) => (selectedId = node.id)"
        @pane-click="selectedId = null"
      >
        <Background :gap="22" :size="1.4" pattern-color="var(--canvas-dot)" />
        <MiniMap pannable zoomable :node-color="() => 'var(--line-strong)'" mask-color="color-mix(in srgb, var(--canvas) 70%, transparent)" />
      </VueFlow>

      <div v-if="legendTypes().length" class="legend">
        <span v-for="ty in legendTypes()" :key="ty" :style="{ '--pin': typeColor(ty) }"><i />{{ ty }}</span>
      </div>

      <div v-if="notice" class="toast" :class="notice.kind" role="status">
        {{ notice.text === "invalid" ? t("error.invalid") : notice.text }}
      </div>
    </main>

    <Inspector class="right" />

    <DebugPanel v-if="debug.enabled" class="bottom" />

    <div v-if="creating" class="modal" @click.self="creating = false">
      <form @submit.prevent="onCreate">
        <h2>{{ t("graph.new") }}</h2>
        <label for="new-id">{{ t("graph.newId") }}</label>
        <input id="new-id" v-model="newId" class="field code" pattern="[a-z0-9][a-z0-9_\-]*" required autofocus />
        <label for="new-name">{{ t("graph.newName") }}</label>
        <input id="new-name" v-model="newName" class="field" />
        <p v-if="newError" class="bad">{{ newError }}</p>
        <div class="actions">
          <button type="button" class="btn ghost" @click="creating = false">{{ t("graph.cancel") }}</button>
          <button type="submit" class="btn primary">{{ t("graph.create") }}</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.boot {
  height: 100%;
  background: var(--canvas);
}

.shell {
  display: grid;
  grid-template-columns: 250px 1fr 320px;
  grid-template-rows: 48px 1fr;
  grid-template-areas:
    "bar bar bar"
    "left canvas right";
  height: 100%;
}

.shell.debugging {
  grid-template-rows: 48px 1fr 220px;
  grid-template-areas:
    "bar bar bar"
    "left canvas right"
    "bottom bottom bottom";
}

.bar {
  grid-area: bar;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}

.left {
  grid-area: left;
}

.right {
  grid-area: right;
}

.bottom {
  grid-area: bottom;
}

.canvas {
  grid-area: canvas;
  position: relative;
  min-width: 0;
  min-height: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 650;
  letter-spacing: -0.01em;
  font-size: 14px;
  margin-right: 6px;
}

.brand svg {
  width: 28px;
  height: 16px;
}

.brand path {
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
}

.brand path:first-of-type {
  stroke: var(--type-audio);
}

.brand path:last-of-type {
  stroke: var(--type-response);
}

.brand circle {
  fill: var(--type-audio);
}

.graph-pick select {
  width: 260px;
}

.sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}

.state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
}

.state i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--faint);
}

.state.live i {
  background: var(--ok);
}

.state.draft i {
  background: var(--warn);
}

.issues {
  font-size: 12px;
  color: var(--muted);
  padding-left: 10px;
  border-left: 1px solid var(--line);
}

.issues.bad {
  color: var(--danger);
}

.issues.warn {
  color: var(--warn);
}

.spacer {
  flex: 1;
}

.sep {
  width: 1px;
  height: 22px;
  background: var(--line);
}

.btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
}

.lang {
  font-size: 11px;
  font-weight: 600;
}

.debug-toggle i {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1.5px solid var(--muted);
}

.debug-toggle.on {
  border-color: var(--ok);
}

.debug-toggle.on i {
  border-color: var(--ok);
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent);
}

.legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  max-width: 60%;
  padding: 6px 10px;
  border-radius: var(--radius-ctl);
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  border: 1px solid var(--line);
  font-family: var(--font-code);
  font-size: 11px;
  color: var(--muted);
}

.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.legend i {
  width: 14px;
  height: 3px;
  border-radius: 2px;
  background: var(--pin);
}

.toast {
  position: absolute;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 14px;
  border-radius: var(--radius-ctl);
  background: var(--panel-raised);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
  max-width: 70%;
}

.toast.error {
  border-color: var(--danger);
  color: var(--danger);
}

.modal {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 0.35);
  z-index: 20;
}

.modal form {
  width: min(360px, calc(100% - 32px));
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 22px;
  border-radius: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}

.modal h2 {
  margin: 0 0 6px;
  font-size: 16px;
}

.modal label {
  font-size: 12px;
  color: var(--muted);
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.bad {
  color: var(--danger);
  margin: 0;
}

@media (max-width: 900px) {
  .shell,
  .shell.debugging {
    grid-template-columns: 1fr;
    grid-template-rows: 48px 1fr 240px;
    grid-template-areas:
      "bar"
      "canvas"
      "right";
  }

  .left,
  .bottom,
  .issues,
  .state {
    display: none;
  }
}
</style>
