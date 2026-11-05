<script setup lang="ts">
import { computed, ref } from "vue";
import { categoryLabel, t } from "../i18n";
import { catalog } from "../store";
import { nodeDescription, nodeTitle } from "../texts";
import { CATEGORY_ORDER, categoryColor, typeColor } from "../types";

const query = ref("");

const groups = computed(() => {
  const q = query.value.trim().toLowerCase();
  const nodes = (catalog.value?.nodes ?? []).filter(
    (n) =>
      !q ||
      n.type.includes(q) ||
      [n.title, nodeTitle(n), nodeDescription(n)].some((text) => text.toLowerCase().includes(q)),
  );
  const byCat = new Map<string, typeof nodes>();
  for (const n of nodes) byCat.set(n.category, [...(byCat.get(n.category) ?? []), n]);
  const rank = (c: string) => (CATEGORY_ORDER.indexOf(c) + 100) % 100;
  return [...byCat.entries()].sort((a, b) => rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]));
});

function onDragStart(event: DragEvent, type: string) {
  event.dataTransfer?.setData("application/flowpilot-node", type);
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

defineEmits<{ add: [type: string] }>();
</script>

<template>
  <aside class="palette">
    <input v-model="query" class="field" type="search" :placeholder="t('palette.search')" :aria-label="t('palette.search')" />
    <p class="hint">{{ t("palette.hint") }}</p>
    <div class="groups">
      <section v-for="[cat, items] in groups" :key="cat" :style="{ '--cat': categoryColor(cat) }">
        <h3>{{ categoryLabel(cat) }}</h3>
        <button
          v-for="n in items"
          :key="n.type"
          class="item"
          draggable="true"
          :title="nodeDescription(n)"
          @dragstart="onDragStart($event, n.type)"
          @dblclick="$emit('add', n.type)"
          @keydown.enter="$emit('add', n.type)"
        >
          <span class="title">{{ nodeTitle(n) }}</span>
          <span class="pins">
            <i v-for="p in n.inputs" :key="'i' + p.name" :class="p.kind" :style="{ '--pin': typeColor(p.type) }" />
            <span class="arrow" />
            <i v-for="p in n.outputs" :key="'o' + p.name" :class="p.kind" :style="{ '--pin': typeColor(p.type) }" />
          </span>
          <span v-if="nodeDescription(n)" class="desc">{{ nodeDescription(n) }}</span>
          <code>{{ n.type }}</code>
        </button>
      </section>
      <p v-if="!groups.length" class="hint">{{ t("palette.empty") }}</p>
    </div>
  </aside>
</template>

<style scoped>
.palette {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  background: var(--panel);
  border-right: 1px solid var(--line);
  min-height: 0;
}

.hint {
  margin: 0 2px 4px;
  color: var(--faint);
  font-size: 12px;
}

.groups {
  overflow-y: auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0 -4px;
  padding: 0 4px;
}

h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}

h3::before {
  content: "";
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--cat);
}

.item {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 8px;
  width: 100%;
  margin-bottom: 4px;
  padding: 7px 9px 7px 11px;
  text-align: left;
  border: 1px solid var(--line);
  border-left: 3px solid var(--cat);
  border-radius: var(--radius-ctl);
  background: var(--panel-raised);
  cursor: grab;
}

.item:hover {
  border-color: var(--line-strong);
  border-left-color: var(--cat);
}

.title {
  font-weight: 500;
}

code {
  grid-column: 1 / -1;
  color: var(--faint);
}

.desc {
  grid-column: 1 / -1;
  color: var(--muted);
  font-size: 11.5px;
  line-height: 1.35;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.pins {
  display: flex;
  align-items: center;
  gap: 3px;
}

.pins i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--pin);
}

.pins i.value {
  border-radius: 1px;
  transform: rotate(45deg);
  width: 6px;
  height: 6px;
}

.arrow {
  width: 8px;
  height: 1px;
  background: var(--faint);
  margin: 0 2px;
}
</style>
