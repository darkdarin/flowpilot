<script setup lang="ts">
import { Background } from "@vue-flow/background";
import { type Connection, VueFlow, useVueFlow } from "@vue-flow/core";
import { MiniMap } from "@vue-flow/minimap";
import { computed, markRaw, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import BrandMark from "./components/BrandMark.vue";
import DebugPanel from "./components/DebugPanel.vue";
import FpNode from "./components/FpNode.vue";
import Inspector from "./components/Inspector.vue";
import CommandsDialog from "./components/CommandsDialog.vue";
import SkillsDialog from "./components/SkillsDialog.vue";
import MemoryDialog from "./components/MemoryDialog.vue";
import SecretsDialog from "./components/SecretsDialog.vue";
import Login from "./components/Login.vue";
import Palette from "./components/Palette.vue";
import TypedEdge from "./components/TypedEdge.vue";
import { type Locale, locale, setLocale, t } from "./i18n";
import {
  addNode,
  api,
  copyNodes,
  duplicateNodes,
  pasteNodes,
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
  makeDefault,
  warnings,
  makeEdge,
  nodes,
  notice,
  openGraph,
  saving,
  selectedId,
  specs,
  token,
  secretsOpen,
  memoryOpen,
  commandsOpen,
  skillsOpen,
} from "./store";
import { connectProblem, edgeType } from "./porttypes";
import { typeColor } from "./types";

const nodeTypes = { fp: markRaw(FpNode) };
const edgeTypes = { typed: markRaw(TypedEdge) };
const {
  screenToFlowCoordinate,
  fitView,
  onNodesInitialized,
  findNode,
  getSelectedNodes,
  addSelectedNodes,
  removeSelectedNodes,
} = useVueFlow();

// The inspector follows selectedId; keep the canvas selection in step when it is set elsewhere
// (debug timeline, a node added from the palette, a renamed node).
watch(selectedId, async (id) => {
  if (!id) return; // nothing chosen in the inspector: the canvas keeps its own selection (⌘A, a paste)
  await nextTick(); // a node added just now is not on the canvas yet
  const current = getSelectedNodes.value;
  if (current.length === 1 && current[0].id === id) return;
  removeSelectedNodes(current);
  const node = id ? findNode(id) : undefined;
  if (node) addSelectedNodes([node]);
});

const booting = ref(true);
const theme = ref(document.documentElement.dataset.theme ?? "dark");
const creating = ref(false);
const newId = ref("");
const newName = ref("");
const newError = ref("");

onMounted(async () => {
  // The saved token first; then no token at all, which works when Home Assistant has signed the
  // user in already (the editor opened from the sidebar through Ingress).
  for (const candidate of token.value ? [token.value, ""] : [""]) {
    try {
      if (await connect(candidate)) break;
    } catch {
      /* fall back to the login form */
    }
  }
  booting.value = false;
});

// fit once per opened graph, after Vue Flow has measured the nodes
let fittedFor: string | null = null;
onNodesInitialized(() => {
  if (fittedFor === graphId.value) return;
  fittedFor = graphId.value;
  void nextTick(() => fitView({ padding: 0.15, maxZoom: 1 }));
});

// The debug panel's height: drag its top edge (or use the arrow keys on it); kept per browser.
const DEBUG_MIN = 120;
const debugHeight = ref(Number(localStorage.getItem("flowpilot.debugHeight")) || 220);

function setDebugHeight(height: number) {
  const max = Math.max(DEBUG_MIN, window.innerHeight - 200);
  debugHeight.value = Math.round(Math.min(max, Math.max(DEBUG_MIN, height)));
  localStorage.setItem("flowpilot.debugHeight", String(debugHeight.value));
}

function startResize(event: PointerEvent) {
  const handle = event.currentTarget as HTMLElement;
  const startY = event.clientY;
  const startHeight = debugHeight.value;
  handle.setPointerCapture(event.pointerId);
  const move = (e: PointerEvent) => setDebugHeight(startHeight + startY - e.clientY);
  const stop = () => {
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", stop);
    handle.removeEventListener("pointercancel", stop);
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
}

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
  return connectProblem(c.source, c.sourceHandle ?? "", c.target, c.targetHandle ?? "") === null;
}

function onConnect(c: Connection) {
  const out = portOf(c.source, c.sourceHandle, "outputs");
  if (!out) return;
  const problem = connectProblem(c.source, c.sourceHandle ?? "", c.target, c.targetHandle ?? "");
  if (problem === "unresolved") return flash(t("error.connectUnresolved"), "error");
  if (problem === "generic") return flash(t("error.connectGeneric"), "error");
  if (problem) return flash(t("error.connect"), "error");
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

// Ctrl+S / ⌘S deploys, like the Deploy button (the browser's "save page" is not useful here).
const deployShortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘S" : "Ctrl+S";

function typing(): boolean {
  const el = document.activeElement as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

/** ⌘A / ⌘C / ⌘V / ⌘D on the canvas: select all, copy, paste and duplicate nodes. */
async function onClipboardKey(event: KeyboardEvent): Promise<boolean> {
  const key = event.key.toLowerCase();
  if (!["a", "c", "v", "d"].includes(key) || !(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return false;
  if (!api.value || typing() || document.querySelector("dialog[open], .modal")) return false;
  if (key === "a") {
    addSelectedNodes(nodes.value.map((n) => findNode(n.id)).filter((n) => n !== undefined));
    selectedId.value = null;
    return true;
  }
  const chosen = getSelectedNodes.value.map((n) => n.id);
  if (key === "c") {
    if (window.getSelection()?.toString()) return false; // copying text the user selected
    const n = copyNodes(chosen);
    if (n) flash(t("clipboard.copied", { n }));
    return n > 0;
  }
  const created = key === "v" ? pasteNodes() : duplicateNodes(chosen);
  if (key === "d") event.preventDefault(); // not the browser's bookmark, even with nothing selected
  if (!created.length) return key === "d";
  await nextTick();
  removeSelectedNodes(getSelectedNodes.value);
  addSelectedNodes(created.map((id) => findNode(id)).filter((n) => n !== undefined));
  selectedId.value = created.length === 1 ? created[0] : null;
  return true;
}

async function onKeydown(event: KeyboardEvent) {
  if (await onClipboardKey(event)) {
    event.preventDefault();
    return;
  }
  if (event.key.toLowerCase() !== "s" || !(event.ctrlKey || event.metaKey) || event.altKey) return;
  if (!api.value) return;
  event.preventDefault();
  await nextTick(); // an edit typed just before the shortcut reaches the autosave watcher first
  if (deploying.value) return;
  // an edit made just now is still being saved: deploy() writes it first
  if (!hasDraft.value && !saving.value) return flash(t("action.nothingToDeploy"));
  if (issues.value.length > 0) return flash(t("error.invalid"), "error");
  void onDeploy();
}

const keydown = (event: KeyboardEvent) => void onKeydown(event);
onMounted(() => window.addEventListener("keydown", keydown));
onBeforeUnmount(() => window.removeEventListener("keydown", keydown));

const isDefault = computed(() => graphs.value.some((g) => g.id === graphId.value && g.default));

async function onMakeDefault() {
  if (!graphId.value) return;
  try {
    await makeDefault(graphId.value);
    flash(t("graph.defaultChanged"));
  } catch (err) {
    flash((err as Error).message, "error");
  }
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
  return [...new Set(edges.value.map((e) => edgeType(e)))].filter((t) => t && !t.includes("|")).sort();
}
</script>

<template>
  <div v-if="booting" class="boot" />
  <Login v-else-if="!api" />
  <div v-else class="shell" :class="{ debugging: debug.enabled }" :style="{ '--debug-height': `${debugHeight}px` }">
    <header class="bar">
      <div class="brand" aria-label="FlowPilot">
        <BrandMark />
        <span>FlowPilot</span>
      </div>

      <label class="graph-pick">
        <span class="sr">{{ t("graph.pick") }}</span>
        <select class="field" :value="graphId ?? ''" @change="onPickGraph">
          <option v-for="g in graphs" :key="g.id" :value="g.id">{{ g.default ? "★ " : "" }}{{ g.name }} ({{ g.id }})</option>
          <option value="__new">+ {{ t("graph.new") }}</option>
        </select>
      </label>
      <button
        class="btn icon ghost star"
        :class="{ on: isDefault }"
        :disabled="isDefault || !live"
        :title="isDefault ? t('graph.isDefault') : live ? t('graph.makeDefault') : t('graph.deployFirst')"
        :aria-label="isDefault ? t('graph.isDefault') : t('graph.makeDefault')"
        :aria-pressed="isDefault"
        @click="onMakeDefault"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.6l-3.8 2 .7-4.3-3.1-3 4.3-.6z" /></svg>
      </button>

      <span class="state" :class="{ draft: hasDraft, live: live && !hasDraft }">
        <i />{{ stateLabel }}
      </span>
      <span class="issues" :class="{ bad: issues.length }">{{ issueLabel }}</span>
      <span v-if="warnings.length" class="issues warn">{{ t("issues.warnings", { n: warnings.length }) }}</span>

      <div class="spacer" />

      <button v-if="hasDraft && live" class="btn ghost" @click="onDiscard">{{ t("action.discard") }}</button>
      <button
        class="btn primary"
        :disabled="!hasDraft || deploying || issues.length > 0"
        :title="`${t('action.deploy')} (${deployShortcut})`"
        :aria-keyshortcuts="deployShortcut === '⌘S' ? 'Meta+S' : 'Control+S'"
        @click="onDeploy"
      >
        {{ deploying ? t("action.deploying") : t("action.deploy") }}
      </button>

      <div class="sep" />

      <button class="btn debug-toggle" :class="{ on: debug.enabled }" :aria-pressed="debug.enabled" @click="debug.enabled = !debug.enabled">
        <i />{{ t("action.debug") }}
      </button>
      <button class="btn icon ghost" :title="t('memory.title')" :aria-label="t('memory.title')" @click="memoryOpen = true">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2.5h8.5l1.5 1.5v9.5H3z" /><path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" /></svg>
      </button>
      <button class="btn icon ghost" :title="t('commands.title')" :aria-label="t('commands.title')" @click="commandsOpen = true">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z" /></svg>
      </button>
      <button class="btn icon ghost" :title="t('skills.title')" :aria-label="t('skills.title')" @click="skillsOpen = true">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.5 2.5h3v3h3v3h-3v3h-3v-3h-3v-3h3z" /></svg>
      </button>
      <button class="btn icon ghost" :title="t('secrets.title')" :aria-label="t('secrets.title')" @click="secretsOpen = true">
        <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="5.5" cy="8" r="3" /><path d="M8.5 8H14.5M12.5 8v2.5M14.5 8v2" /></svg>
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
        :pan-on-scroll="true"
        :zoom-on-scroll="false"
        delete-key-code="Delete"
        @connect="onConnect"
        @node-click="({ node }) => (selectedId = node.id)"
        @node-drag-start="({ node }) => (selectedId = node.id)"
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
    <div
      v-if="debug.enabled"
      class="resizer"
      role="separator"
      aria-orientation="horizontal"
      tabindex="0"
      :aria-label="t('debug.resize')"
      :title="t('debug.resize')"
      :aria-valuenow="debugHeight"
      :aria-valuemin="DEBUG_MIN"
      @pointerdown.prevent="startResize"
      @dblclick="setDebugHeight(220)"
      @keydown.up.prevent="setDebugHeight(debugHeight + 20)"
      @keydown.down.prevent="setDebugHeight(debugHeight - 20)"
    />

    <SecretsDialog v-if="secretsOpen" />
    <MemoryDialog v-if="memoryOpen" />
    <CommandsDialog v-if="commandsOpen" />
    <SkillsDialog v-if="skillsOpen" />

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
  /* as tall as Home Assistant's header (--header-height), so the bar lines up with its sidebar */
  --bar-height: 56px;
  /* the node list, and the turn list of the debug panel under it */
  --left-width: 250px;
  display: grid;
  grid-template-columns: var(--left-width) 1fr 320px;
  grid-template-rows: var(--bar-height) 1fr;
  grid-template-areas:
    "bar bar bar"
    "left canvas right";
  height: 100%;
}

.shell.debugging {
  grid-template-rows: var(--bar-height) 1fr var(--debug-height, 220px);
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

.resizer {
  grid-area: bottom;
  align-self: start;
  z-index: 6;
  height: 7px;
  margin-top: -4px;
  cursor: row-resize;
  touch-action: none;
}

.resizer:hover,
.resizer:focus-visible,
.resizer:active {
  outline: none;
  background: linear-gradient(transparent 2px, var(--focus) 2px 5px, transparent 5px);
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

.brand .brand-mark {
  width: 22px;
  height: 18px;
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

.star svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linejoin: round;
}

.star.on {
  color: var(--warn);
  opacity: 1;
}

.star.on svg {
  fill: currentColor;
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
    grid-template-rows: var(--bar-height) 1fr 240px;
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
