<script setup lang="ts">
// The assistant's own data and connections, kept in the runtime for every graph: one labelled
// menu instead of a row of icon-only buttons.
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import type { Key } from "../i18n";
import { t } from "../i18n";
import { commandsOpen, mcpOpen, memoryOpen, secretsOpen, skillsOpen, voicesOpen } from "../store";

interface Item {
  title: Key;
  hint: Key;
  open: () => void;
  icon: string; // svg paths, 16×16, stroked like the other icons
}

const items: Item[] = [
  { title: "memory.title", hint: "menu.memoryHint", open: () => (memoryOpen.value = true), icon: '<path d="M3 2.5h8.5l1.5 1.5v9.5H3z" /><path d="M5.5 6h5M5.5 8.5h5M5.5 11h3" />' },
  { title: "voices.title", hint: "menu.voicesHint", open: () => (voicesOpen.value = true), icon: '<rect x="6" y="2" width="4" height="7.5" rx="2" /><path d="M3.5 8a4.5 4.5 0 0 0 9 0M8 12.5V14.5" />' },
  { title: "commands.title", hint: "menu.commandsHint", open: () => (commandsOpen.value = true), icon: '<path d="M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z" />' },
  { title: "skills.title", hint: "menu.skillsHint", open: () => (skillsOpen.value = true), icon: '<path d="M6.5 2.5h3v3h3v3h-3v3h-3v-3h-3v-3h3z" />' },
  { title: "mcp.title", hint: "menu.mcpHint", open: () => (mcpOpen.value = true), icon: '<path d="M5.5 2v3.5M10.5 2v3.5M3.5 5.5h9v2a4.5 4.5 0 0 1-9 0zM8 12v2.5" />' },
  { title: "secrets.title", hint: "menu.secretsHint", open: () => (secretsOpen.value = true), icon: '<circle cx="5.5" cy="8" r="3" /><path d="M8.5 8H14.5M12.5 8v2.5M14.5 8v2" />' },
];  // prettier-ignore

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const list = ref<HTMLElement | null>(null);

async function toggle() {
  open.value = !open.value;
  if (open.value) {
    await nextTick();
    list.value?.querySelector<HTMLElement>("button")?.focus();
  }
}

function choose(item: Item) {
  open.value = false;
  item.open();
}

function onKey(event: KeyboardEvent) {
  if (!open.value) return;
  const buttons = [...(list.value?.querySelectorAll<HTMLElement>("button") ?? [])];
  const at = buttons.indexOf(document.activeElement as HTMLElement);
  if (event.key === "Escape") {
    open.value = false;
    root.value?.querySelector<HTMLElement>(".trigger")?.focus();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    buttons[(at + step + buttons.length) % buttons.length]?.focus();
  }
}

function onOutside(event: MouseEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) open.value = false;
}

onMounted(() => document.addEventListener("mousedown", onOutside));
onBeforeUnmount(() => document.removeEventListener("mousedown", onOutside));
</script>

<template>
  <div ref="root" class="assistant-menu" @keydown="onKey">
    <button class="btn ghost trigger" :class="{ on: open }" aria-haspopup="menu" :aria-expanded="open" @click="toggle">
      <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="5.5" r="2.5" /><path d="M3 13.5a5 5 0 0 1 10 0" /></svg>
      {{ t("menu.assistant") }}
      <svg class="caret" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 6.5 8 10l3.5-3.5" /></svg>
    </button>
    <div v-if="open" ref="list" class="list" role="menu" :aria-label="t('menu.assistant')">
      <button v-for="item in items" :key="item.title" class="item" role="menuitem" @click="choose(item)">
        <svg viewBox="0 0 16 16" aria-hidden="true" v-html="item.icon" />
        <span class="text">
          <span class="title">{{ t(item.title) }}</span>
          <span class="hint">{{ t(item.hint) }}</span>
        </span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.assistant-menu {
  position: relative;
}

.trigger svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.trigger .caret {
  width: 12px;
  height: 12px;
  opacity: 0.7;
}

.trigger.on {
  background: var(--panel-raised);
  border-color: var(--line);
}

.list {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 30;
  width: 300px;
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
}

.item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.item:hover,
.item:focus-visible {
  background: var(--panel-raised);
  outline: none;
}

.item svg {
  flex: none;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.text {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.title {
  font-weight: 600;
  font-size: 13px;
}

.hint {
  font-size: 11.5px;
  color: var(--muted);
  line-height: 1.35;
}
</style>
