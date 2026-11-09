<script setup lang="ts">
// The menu that opens where a wire dragged from a port was dropped on empty canvas: the nodes
// that fit that port. Picking one adds it there, wired.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { NodeInfo, PortInfo } from "../api";
import { categoryLabel, t, type Key } from "../i18n";
import { nodeDescription, nodeTitle } from "../texts";
import { ROLE_ORDER, categoryColor, nodeRole, typeColor, typeShort } from "../types";

export interface Choice {
  node: NodeInfo;
  port: PortInfo; // of the new node, the one the wire goes to
}

const props = defineProps<{ x: number; y: number; choices: Choice[] }>();
const emit = defineEmits<{ pick: [choice: Choice]; close: [] }>();

const query = ref("");
const active = ref(0);
const box = ref<HTMLElement | null>(null);
const search = ref<HTMLInputElement | null>(null);

const shown = computed(() => {
  const q = query.value.trim().toLowerCase();
  const rank = (c: Choice) => (ROLE_ORDER.indexOf(nodeRole(c.node)) + 100) % 100;
  return props.choices
    .filter((c) => !q || c.node.type.includes(q) || [nodeTitle(c.node), nodeDescription(c.node)].some((s) => s.toLowerCase().includes(q)))
    .sort((a, b) => rank(a) - rank(b) || nodeTitle(a.node).localeCompare(nodeTitle(b.node)));
});

watch(query, () => (active.value = 0));

function roleLabel(role: string): string {
  return ["stt", "tts", "audio"].includes(role) ? t(`role.${role}` as Key) : categoryLabel(role);
}

function roleColor(role: string): string {
  return categoryColor(["stt", "tts", "audio"].includes(role) ? "speech" : role);
}

function onKey(event: KeyboardEvent) {
  if (event.key === "Escape") emit("close");
  else if (event.key === "ArrowDown") active.value = Math.min(shown.value.length - 1, active.value + 1);
  else if (event.key === "ArrowUp") active.value = Math.max(0, active.value - 1);
  else if (event.key === "Enter" && shown.value[active.value]) emit("pick", shown.value[active.value]);
  else return;
  event.preventDefault();
  void nextTick(() => box.value?.querySelector(".choice.on")?.scrollIntoView({ block: "nearest" }));
}

// a click anywhere else closes it
function onPointerDown(event: PointerEvent) {
  if (box.value && !box.value.contains(event.target as Node)) emit("close");
}

// keep it inside the canvas
const position = ref({ left: props.x, top: props.y });
onMounted(async () => {
  document.addEventListener("pointerdown", onPointerDown, true);
  await nextTick();
  search.value?.focus();
  const el = box.value;
  const parent = el?.offsetParent as HTMLElement | null;
  if (!el || !parent) return;
  position.value = {
    left: Math.max(8, Math.min(props.x, parent.clientWidth - el.offsetWidth - 8)),
    top: Math.max(8, Math.min(props.y, parent.clientHeight - el.offsetHeight - 8)),
  };
});
onBeforeUnmount(() => document.removeEventListener("pointerdown", onPointerDown, true));
</script>

<template>
  <div
    ref="box"
    class="connect-menu nodrag nowheel"
    :style="{ left: `${position.left}px`, top: `${position.top}px` }"
    role="dialog"
    :aria-label="t('connectMenu.title')"
    @keydown="onKey"
  >
    <input ref="search" v-model="query" class="field" type="search" :placeholder="t('connectMenu.search')" :aria-label="t('connectMenu.search')" />
    <ul v-if="shown.length" role="listbox">
      <li
        v-for="(c, i) in shown"
        :key="c.node.type"
        class="choice"
        :class="{ on: i === active }"
        role="option"
        :aria-selected="i === active"
        :style="{ '--cat': roleColor(nodeRole(c.node)) }"
        @pointerenter="active = i"
        @click="emit('pick', c)"
      >
        <span class="name">{{ nodeTitle(c.node) }}</span>
        <span class="port">
          <i :class="c.port.kind" :style="{ '--pin': typeColor(c.port.type) }" />{{ c.port.name }}
          <code>{{ typeShort(c.port.type) }}</code>
        </span>
        <span class="role">{{ roleLabel(nodeRole(c.node)) }}</span>
      </li>
    </ul>
    <p v-else class="empty">{{ t("connectMenu.empty") }}</p>
  </div>
</template>

<style scoped>
.connect-menu {
  position: absolute;
  z-index: 20;
  width: 300px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  background: var(--panel-raised);
  box-shadow: var(--shadow);
}

ul {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}

.choice {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 1px 8px;
  padding: 6px 8px 6px 10px;
  border-left: 3px solid var(--cat);
  border-radius: 6px;
  cursor: pointer;
}

.choice.on {
  background: color-mix(in srgb, var(--cat) 14%, transparent);
}

.name {
  font-weight: 500;
}

.role {
  grid-row: 1;
  grid-column: 2;
  color: var(--faint);
  font-size: 11px;
}

.port {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--muted);
  font-size: 11.5px;
}

.port code {
  color: var(--faint);
}

.port i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--pin);
}

.port i.value {
  border-radius: 1px;
  transform: rotate(45deg);
  width: 6px;
  height: 6px;
}

.empty {
  margin: 4px;
  color: var(--muted);
  font-size: 12px;
}
</style>
