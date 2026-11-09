<script lang="ts">
import type { NodeInfo, PortInfo } from "../api";

export interface Choice {
  node: NodeInfo;
  port: PortInfo; // of the new node, the one the wire goes to
}

// the new node's place, as App adds it (flow units): its left edge just right of the drop for a
// wire from an output, or ending just left of it for a wire to an input
export const GHOST = { width: 260, height: 96, gap: 16, top: 70 };
</script>

<script setup lang="ts">
// Where a wire dragged from a port was dropped on empty canvas: the wire stays drawn, a ghost shows
// where the new node goes, and a menu offers the nodes that fit the port. Its first level is the
// kinds of node (speech-to-text, text-to-speech, audio, agents, logic, debug; only kinds with a
// fitting node), the second the nodes of one kind; typing searches every node at once.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { categoryLabel, t, type Key } from "../i18n";
import { nodeDescription, nodeTitle } from "../texts";
import { ROLE_ORDER, categoryColor, nodeRole, typeColor, typeShort } from "../types";

const props = defineProps<{
  x: number; // where the wire was dropped, in the canvas
  y: number;
  hx: number; // the port it comes from
  hy: number;
  side: "inputs" | "outputs"; // of the port it comes from
  zoom: number;
  color: string;
  choices: Choice[];
}>();
const emit = defineEmits<{ pick: [choice: Choice]; close: [] }>();

type Row = { kind: "role"; role: string; count: number } | { kind: "node"; choice: Choice };

const query = ref("");
const level = ref<string | null>(null); // the kind opened, or the kinds
const active = ref(0);
const box = ref<HTMLElement | null>(null);
const search = ref<HTMLInputElement | null>(null);

const rank = (role: string) => (ROLE_ORDER.indexOf(role) + 100) % 100;
const byName = (a: Choice, b: Choice) =>
  rank(nodeRole(a.node)) - rank(nodeRole(b.node)) || nodeTitle(a.node).localeCompare(nodeTitle(b.node));

const roles = computed(() => {
  const count = new Map<string, number>();
  for (const c of props.choices) count.set(nodeRole(c.node), (count.get(nodeRole(c.node)) ?? 0) + 1);
  return [...count.entries()].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]));
});

// one kind only: open it right away
if (roles.value.length === 1) level.value = roles.value[0][0];

const rows = computed<Row[]>(() => {
  const q = query.value.trim().toLowerCase();
  if (q) {
    return props.choices
      .filter((c) => c.node.type.includes(q) || [nodeTitle(c.node), nodeDescription(c.node)].some((s) => s.toLowerCase().includes(q)))
      .sort(byName)
      .map((choice) => ({ kind: "node", choice }));
  }
  if (level.value) {
    return props.choices
      .filter((c) => nodeRole(c.node) === level.value)
      .sort(byName)
      .map((choice) => ({ kind: "node", choice }));
  }
  return roles.value.map(([role, count]) => ({ kind: "role", role, count }));
});

const current = computed(() => rows.value[active.value]);
const ghost = computed(() => (current.value?.kind === "node" ? current.value.choice : null));

watch([query, level], () => (active.value = 0));

function roleLabel(role: string): string {
  return ["stt", "tts", "audio"].includes(role) ? t(`role.${role}` as Key) : categoryLabel(role);
}

function roleColor(role: string): string {
  return categoryColor(["stt", "tts", "audio"].includes(role) ? "speech" : role);
}

function choose(row: Row | undefined) {
  if (!row) return;
  if (row.kind === "role") {
    level.value = row.role;
    search.value?.focus();
  } else emit("pick", row.choice);
}

function back() {
  if (roles.value.length > 1) level.value = null;
  search.value?.focus();
}

function onKey(event: KeyboardEvent) {
  const typed = query.value !== "";
  if (event.key === "Escape") emit("close");
  else if (event.key === "ArrowDown") active.value = Math.min(rows.value.length - 1, active.value + 1);
  else if (event.key === "ArrowUp") active.value = Math.max(0, active.value - 1);
  else if (event.key === "Enter") choose(current.value);
  else if (event.key === "ArrowRight" && !typed && current.value?.kind === "role") choose(current.value);
  else if ((event.key === "ArrowLeft" || event.key === "Backspace") && !typed && level.value) back();
  else return;
  event.preventDefault();
  void nextTick(() => box.value?.querySelector(".row.on")?.scrollIntoView({ block: "nearest" }));
}

// a click anywhere else closes it
function onPointerDown(event: PointerEvent) {
  if (box.value && !box.value.contains(event.target as Node)) emit("close");
}

// the wire as Vue Flow draws it while dragging: a bezier from the port to the drop point
const ghostBox = computed(() => {
  const z = props.zoom;
  const left = props.side === "outputs" ? props.x + GHOST.gap * z : props.x - (GHOST.gap + GHOST.width) * z;
  return { left, top: props.y - GHOST.top * z };
});

// it ends on the ghost's edge, where the new node's port will be
const wire = computed(() => {
  const { y, hx, hy, side } = props;
  const x = side === "outputs" ? ghostBox.value.left : ghostBox.value.left + GHOST.width * props.zoom;
  const dx = Math.max(40 * props.zoom, Math.abs(x - hx) / 2);
  const sign = side === "outputs" ? 1 : -1;
  return `M ${hx},${hy} C ${hx + sign * dx},${hy} ${x - sign * dx},${y} ${x},${y}`;
});



// the menu under the ghost, kept inside the canvas
const place = ref({ left: 0, top: 0 });
function layout() {
  const el = box.value;
  const parent = el?.parentElement;
  if (!el || !parent) return;
  const left = ghostBox.value.left;
  const top = ghostBox.value.top + GHOST.height * props.zoom + 10;
  place.value = {
    left: Math.max(8, Math.min(left, parent.clientWidth - el.offsetWidth - 8)),
    top: Math.max(8, Math.min(top, parent.clientHeight - el.offsetHeight - 8)),
  };
}

onMounted(async () => {
  document.addEventListener("pointerdown", onPointerDown, true);
  await nextTick();
  layout();
  search.value?.focus();
});
onBeforeUnmount(() => document.removeEventListener("pointerdown", onPointerDown, true));
watch(rows, () => void nextTick(layout));
</script>

<template>
  <div class="connect-layer">
    <svg class="wire" aria-hidden="true">
      <path :d="wire" :style="{ stroke: color, strokeWidth: 2.5 * zoom, strokeDasharray: `${6 * zoom} ${5 * zoom}` }" />
    </svg>

    <div
      class="ghost"
      :style="{ left: `${ghostBox.left}px`, top: `${ghostBox.top}px`, transform: `scale(${zoom})`, '--cat': ghost ? roleColor(nodeRole(ghost.node)) : 'var(--line-strong)' }"
      aria-hidden="true"
    >
      <div class="ghost-head">{{ ghost ? nodeTitle(ghost.node) : "?" }}</div>
      <div v-if="ghost" class="ghost-port" :class="side === 'outputs' ? 'in' : 'out'">
        <i :class="ghost.port.kind" :style="{ '--pin': typeColor(ghost.port.type) }" />{{ ghost.port.name }}
      </div>
    </div>

    <div
      ref="box"
      class="connect-menu nodrag nowheel"
      :style="{ left: `${place.left}px`, top: `${place.top}px` }"
      role="dialog"
      :aria-label="t('connectMenu.title')"
      @keydown="onKey"
    >
      <input ref="search" v-model="query" class="field" type="search" :placeholder="t('connectMenu.search')" :aria-label="t('connectMenu.search')" />
      <button v-if="level && !query && roles.length > 1" class="back" :style="{ '--cat': roleColor(level) }" @click="back">
        ‹ {{ roleLabel(level) }}
      </button>
      <ul v-if="rows.length" role="listbox">
        <li
          v-for="(row, i) in rows"
          :key="row.kind === 'role' ? `r:${row.role}` : row.choice.node.type"
          class="row"
          :class="[row.kind, { on: i === active }]"
          role="option"
          :aria-selected="i === active"
          :style="{ '--cat': roleColor(row.kind === 'role' ? row.role : nodeRole(row.choice.node)) }"
          @pointerenter="active = i"
          @click="choose(row)"
        >
          <template v-if="row.kind === 'role'">
            <span class="name">{{ roleLabel(row.role) }}</span>
            <span class="count">{{ row.count }} ›</span>
          </template>
          <template v-else>
            <span class="name">{{ nodeTitle(row.choice.node) }}</span>
            <span v-if="query" class="role">{{ roleLabel(nodeRole(row.choice.node)) }}</span>
            <span class="port">
              <i :class="row.choice.port.kind" :style="{ '--pin': typeColor(row.choice.port.type) }" />{{ row.choice.port.name }}
              <code>{{ typeShort(row.choice.port.type) }}</code>
            </span>
          </template>
        </li>
      </ul>
      <p v-else class="empty">{{ t("connectMenu.empty") }}</p>
    </div>
  </div>
</template>

<style scoped>
.connect-layer {
  position: absolute;
  inset: 0;
  z-index: 20;
  pointer-events: none;
  overflow: hidden;
}

.wire {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.wire path {
  fill: none;
}

.ghost {
  position: absolute;
  width: 260px;
  height: 96px;
  transform-origin: top left;
  border: 1.5px dashed var(--cat);
  border-radius: var(--radius-node, 10px);
  background: color-mix(in srgb, var(--node) 55%, transparent);
  opacity: 0.85;
}

/* the port row's middle is GHOST.top (70) below the ghost's top: where the wire ends */
.ghost-head {
  box-sizing: border-box;
  height: 46px;
  padding: 13px 14px;
  border-bottom: 1px dashed var(--line);
  border-left: 3px solid var(--cat);
  border-radius: 9px 9px 0 0;
  font-weight: 600;
  font-size: 13px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ghost-port {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 48px;
  padding: 0 14px;
  font-size: 12.5px;
  color: var(--muted);
}

.ghost-port.out {
  justify-content: flex-end;
}

.ghost-port.in i {
  margin-left: -19px; /* on the node's edge, where the handle is */
}

.ghost-port.out i {
  order: 2;
  margin-right: -19px;
}

.ghost-port i,
.port i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--pin);
}

.ghost-port i.value,
.port i.value {
  border-radius: 1px;
  transform: rotate(45deg);
  width: 8px;
  height: 8px;
}

.connect-menu {
  position: absolute;
  pointer-events: auto;
  width: 290px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--panel-raised);
  box-shadow: var(--shadow);
}

.back {
  align-self: flex-start;
  padding: 2px 8px 2px 6px;
  border: 0;
  border-radius: 6px;
  background: color-mix(in srgb, var(--cat) 14%, transparent);
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

ul {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 300px;
  overflow-y: auto;
}

.row {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: baseline;
  gap: 1px 8px;
  padding: 6px 8px 6px 10px;
  border-left: 3px solid var(--cat);
  border-radius: 6px;
  cursor: pointer;
}

.row {
  color: var(--text);
}

.row.on {
  background: color-mix(in srgb, var(--cat) 14%, transparent);
}

.name {
  font-weight: 500;
}

.count,
.role {
  color: var(--faint);
  font-size: 11.5px;
  white-space: nowrap;
}

.port {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--muted);
  font-size: 11.5px;
}

.port i {
  width: 7px;
  height: 7px;
}

.port i.value {
  width: 6px;
  height: 6px;
}

.port code {
  color: var(--faint);
}

.empty {
  margin: 4px;
  color: var(--muted);
  font-size: 12px;
}
</style>
