<script setup lang="ts">
// A select for settings with known choices: a readable name first and the raw value under it,
// "Default" for optional settings and "Enter manually…" for a value that is not in the list.
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { t } from "../i18n";

export interface PickerOption {
  id: string;
  name?: string;
  description?: string;
}

const props = defineProps<{
  inputId: string;
  modelValue: string;
  options: PickerOption[];
  emptyLabel?: string; // shown for "" when the setting is optional; absent = required
  allowCustom?: boolean;
  loading?: boolean;
  error?: string;
  warn?: boolean;
  invalid?: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

const open = ref(false);
const editing = ref(false);
const draft = ref("");
const active = ref(0);
const root = ref<HTMLElement | null>(null);
const listEl = ref<HTMLElement | null>(null);
const searchEl = ref<HTMLInputElement | null>(null);
const query = ref("");
const searchable = computed(() => props.options.length > 8);
const visible = computed(() => {
  const q = query.value.trim().toLowerCase();
  if (!q) return props.options;
  return props.options.filter((o) =>
    [o.id, o.name ?? "", o.description ?? ""].some((text) => text.toLowerCase().includes(q)),
  );
});
const editEl = ref<HTMLInputElement | null>(null);

const current = computed(() => props.options.find((o) => o.id === props.modelValue));
const isCustom = computed(() => props.modelValue !== "" && !current.value);

type Row =
  | { kind: "empty" }
  | { kind: "option"; option: PickerOption }
  | { kind: "custom-current" }
  | { kind: "custom" };

const rows = computed<Row[]>(() => {
  const list: Row[] = [];
  if (props.emptyLabel !== undefined) list.push({ kind: "empty" });
  if (isCustom.value) list.push({ kind: "custom-current" });
  for (const option of visible.value) list.push({ kind: "option", option });
  if (props.allowCustom) list.push({ kind: "custom" });
  return list;
});

function selectedIndex(): number {
  const i = rows.value.findIndex((r) =>
    r.kind === "option" ? r.option.id === props.modelValue
    : r.kind === "custom-current" ? true
    : r.kind === "empty" ? props.modelValue === "" : false,
  );
  return Math.max(0, i);
}

function toggle() {
  if (open.value) return close();
  open.value = true;
  query.value = "";
  active.value = selectedIndex();
  void nextTick(() => {
    scrollActive();
    searchEl.value?.focus();
  });
}

function close() {
  open.value = false;
}

function choose(row: Row) {
  if (row.kind === "empty") emit("update:modelValue", "");
  else if (row.kind === "option") emit("update:modelValue", row.option.id);
  else if (row.kind === "custom" || row.kind === "custom-current") return startEditing();
  close();
}

function startEditing() {
  close();
  editing.value = true;
  draft.value = props.modelValue;
  void nextTick(() => editEl.value?.focus());
}

function commit() {
  if (!editing.value) return;
  editing.value = false;
  emit("update:modelValue", draft.value.trim());
}

function onKey(event: KeyboardEvent) {
  if (!open.value) {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      toggle();
    }
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    active.value = (active.value + step + rows.value.length) % rows.value.length;
    scrollActive();
  } else if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    const row = rows.value[active.value];
    if (row) choose(row);
  } else if (event.key === "Tab") close();
}

function scrollActive() {
  listEl.value?.querySelectorAll("li[role=option]")[active.value]?.scrollIntoView({ block: "nearest" });
}

function onOutside(event: MouseEvent) {
  if (root.value && !root.value.contains(event.target as Node)) close();
}

watch(query, () => (active.value = Math.max(0, rows.value.findIndex((r) => r.kind === "option"))));

watch(open, (value) => {
  if (value) document.addEventListener("mousedown", onOutside);
  else document.removeEventListener("mousedown", onOutside);
});
onBeforeUnmount(() => document.removeEventListener("mousedown", onOutside));

function label(option: PickerOption) {
  return option.name || option.id;
}
</script>

<template>
  <div ref="root" class="picker">
    <input
      v-if="editing"
      :id="inputId"
      ref="editEl"
      v-model="draft"
      class="field"
      :placeholder="t('picker.customHint')"
      @keydown.enter.prevent="commit"
      @keydown.escape.prevent="editing = false"
      @blur="commit"
    />
    <button
      v-else
      :id="inputId"
      type="button"
      class="field trigger"
      :class="{ warn, invalid, open }"
      role="combobox"
      :aria-expanded="open"
      aria-haspopup="listbox"
      @click="toggle"
      @keydown="onKey"
    >
      <span class="value">
        <template v-if="current">
          <span class="name">{{ label(current) }}</span>
          <span v-if="current.name && current.name !== current.id" class="id">{{ current.id }}</span>
        </template>
        <template v-else-if="isCustom">
          <span class="name">{{ modelValue }}</span>
          <span class="id">{{ t("picker.customValue") }}</span>
        </template>
        <span v-else class="placeholder">{{ emptyLabel ?? t("picker.unset") }}</span>
      </span>
      <svg class="caret" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" /></svg>
    </button>

    <ul v-if="open" ref="listEl" class="list" role="listbox">
      <li v-if="searchable" class="search">
        <input
          ref="searchEl"
          v-model="query"
          class="field"
          type="search"
          :placeholder="t('picker.search')"
          :aria-label="t('picker.search')"
          @keydown="onKey"
        />
      </li>
      <li
        v-for="(row, i) in rows"
        :key="row.kind === 'option' ? 'o:' + row.option.id : row.kind"
        role="option"
        :aria-selected="i === selectedIndex()"
        :class="{ active: i === active, selected: i === selectedIndex(), extra: row.kind !== 'option' }"
        @mousedown.prevent="choose(row)"
        @mousemove="active = i"
      >
        <template v-if="row.kind === 'option'">
          <span class="name">{{ label(row.option) }}</span>
          <span v-if="row.option.description || (row.option.name && row.option.name !== row.option.id)" class="id">
            {{ row.option.description || row.option.id }}
          </span>
        </template>
        <template v-else-if="row.kind === 'empty'">
          <span class="name">{{ emptyLabel }}</span>
        </template>
        <template v-else-if="row.kind === 'custom-current'">
          <span class="name">{{ modelValue }}</span>
          <span class="id">{{ t("picker.customValue") }}</span>
        </template>
        <template v-else>
          <span class="name">{{ t("picker.custom") }}</span>
        </template>
      </li>
      <li v-if="loading" class="note">{{ t("picker.loading") }}</li>
      <li v-else-if="error" class="note bad">{{ t("picker.unreachable", { msg: error }) }}</li>
      <li v-else-if="!options.length" class="note">{{ t("picker.none") }}</li>
    </ul>
  </div>
</template>

<style scoped>
.picker {
  position: relative;
}

.trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  height: auto;
  min-height: 30px;
  padding: 4px 8px 4px 9px;
  text-align: left;
  cursor: pointer;
}

.trigger.open {
  border-color: var(--focus);
}

.trigger.warn {
  border-color: var(--warn);
}

.trigger.invalid {
  border-color: var(--danger);
}

.value {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  line-height: 1.3;
}

.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.id {
  font-family: var(--font-code);
  font-size: 11px;
  color: var(--faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.placeholder {
  color: var(--faint);
}

.caret {
  flex: none;
  width: 12px;
  height: 12px;
  fill: none;
  stroke: var(--muted);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.list {
  position: absolute;
  z-index: 30;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 280px;
  overflow-y: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-ctl);
  background: var(--panel-raised);
  box-shadow: var(--shadow);
}

.list li {
  display: flex;
  flex-direction: column;
  padding: 6px 8px;
  border-radius: 5px;
  cursor: pointer;
  line-height: 1.3;
}

.list li.active {
  background: color-mix(in srgb, var(--focus) 14%, transparent);
}

.list li.selected .name {
  font-weight: 600;
}

.list li.extra .name {
  color: var(--muted);
}

.list li.search {
  padding: 2px 2px 6px;
  cursor: default;
}

.list li.search .field {
  height: 28px;
}

.list li.note {
  cursor: default;
  color: var(--faint);
  font-size: 12px;
}

.list li.note.bad {
  color: var(--danger);
}
</style>
