<script setup lang="ts">
// The runtime's long-term memory: facts the assistant knows about the home and each person. Facts
// are grouped by owner (the home first, then people); each can be edited, moved or forgotten.
import { computed, onMounted, ref } from "vue";
import type { Fact, MemoryList } from "../api";
import { t } from "../i18n";
import { api, flash, memoryOpen } from "../store";

const HOME = ""; // the select's value for "the home" (owner null)

const data = ref<MemoryList>({ facts: [], people: [], people_known: false });
const busy = ref(false);
const newText = ref("");
const newOwner = ref(HOME);
const editing = ref<string | null>(null);
const editText = ref("");
const editOwner = ref(HOME);

const names = computed(() => new Map(data.value.people.map((p) => [p.id, p.name])));

function ownerName(owner: string | null): string {
  if (owner === null) return t("memory.home");
  return names.value.get(owner) ?? t("memory.unknownPerson", { id: owner });
}

/** Owners to pick from: the home, the people Home Assistant lists, and owners of existing facts. */
const owners = computed(() => {
  const ids = [...data.value.people.map((p) => p.id)];
  for (const f of data.value.facts) if (f.owner !== null && !ids.includes(f.owner)) ids.push(f.owner);
  return [{ id: HOME, name: t("memory.home") }, ...ids.map((id) => ({ id, name: ownerName(id) }))];
});

const groups = computed(() => {
  const byOwner = new Map<string | null, Fact[]>();
  for (const f of data.value.facts) byOwner.set(f.owner, [...(byOwner.get(f.owner) ?? []), f]);
  return [...byOwner.entries()]
    .sort(([a], [b]) => (a === null ? -1 : b === null ? 1 : ownerName(a).localeCompare(ownerName(b))))
    .map(([owner, facts]) => ({ owner, name: ownerName(owner), facts }));
});

async function run(action: () => Promise<unknown>) {
  busy.value = true;
  try {
    await action();
    data.value = await api.value!.memory();
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = false;
  }
}

onMounted(() => run(async () => undefined));

function add() {
  const text = newText.value.trim();
  if (!text) return;
  void run(async () => {
    await api.value!.addFact(text, newOwner.value || null);
    newText.value = "";
  });
}

function startEdit(f: Fact) {
  editing.value = f.id;
  editText.value = f.text;
  editOwner.value = f.owner ?? HOME;
}

function saveEdit(id: string) {
  const text = editText.value.trim();
  if (!text) return;
  void run(async () => {
    await api.value!.updateFact(id, text, editOwner.value || null);
    editing.value = null;
  });
}

function forget(f: Fact) {
  if (window.confirm(t("memory.confirmRemove", { text: f.text }))) void run(() => api.value!.deleteFact(f.id));
}
</script>

<template>
  <div class="modal" @click.self="memoryOpen = false" @keydown.esc="memoryOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('memory.title')">
      <h2>{{ t("memory.title") }}</h2>
      <p class="hint">{{ t("memory.hint") }}</p>

      <form class="add" @submit.prevent="add">
        <input v-model="newText" class="field" maxlength="500" :placeholder="t('memory.placeholder')" autofocus />
        <select v-model="newOwner" class="field owner">
          <option v-for="o in owners" :key="o.id" :value="o.id">{{ o.name }}</option>
        </select>
        <button type="submit" class="btn primary small" :disabled="!newText.trim() || busy">{{ t("memory.add") }}</button>
      </form>
      <p v-if="!data.people_known" class="hint">{{ t("memory.noPeople") }}</p>

      <template v-if="groups.length">
        <section v-for="g in groups" :key="g.owner ?? HOME" class="group">
          <h3>{{ g.name }}</h3>
          <ul class="list">
            <li v-for="f in g.facts" :key="f.id">
              <form v-if="editing === f.id" class="edit" @submit.prevent="saveEdit(f.id)">
                <input v-model="editText" class="field" maxlength="500" autofocus />
                <select v-model="editOwner" class="field owner">
                  <option v-for="o in owners" :key="o.id" :value="o.id">{{ o.name }}</option>
                </select>
                <button type="button" class="btn ghost small" @click="editing = null">{{ t("graph.cancel") }}</button>
                <button type="submit" class="btn primary small" :disabled="!editText.trim() || busy">{{ t("memory.save") }}</button>
              </form>
              <div v-else class="row">
                <span class="text">{{ f.text }}</span>
                <span v-if="f.source !== 'editor'" class="source">{{ t("memory.byAgent") }}</span>
                <button type="button" class="btn ghost small" @click="startEdit(f)">{{ t("memory.edit") }}</button>
                <button type="button" class="btn ghost small danger" :disabled="busy" @click="forget(f)">{{ t("memory.remove") }}</button>
              </div>
            </li>
          </ul>
        </section>
      </template>
      <p v-else class="hint">{{ t("memory.none") }}</p>

      <div class="actions">
        <button type="button" class="btn ghost" @click="memoryOpen = false">{{ t("secrets.close") }}</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.modal {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgb(0 0 0 / 0.35);
  z-index: 20;
}

.dialog {
  width: min(640px, calc(100% - 32px));
  max-height: calc(100% - 64px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 22px;
  border-radius: 12px;
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}

h2 {
  margin: 0;
  font-size: 16px;
}

h3 {
  margin: 4px 0 6px;
  font-size: 13px;
  font-weight: 600;
}

.hint {
  margin: 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.list li {
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-raised);
}

.row,
.edit,
.add {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.add .field,
.edit .field {
  flex: 1;
  min-width: 180px;
}

.field.owner {
  flex: 0 1 170px;
  min-width: 120px;
}

.text {
  flex: 1;
  min-width: 180px;
  font-size: 13px;
  line-height: 1.4;
}

.source {
  font-size: 11.5px;
  padding: 0 7px;
  line-height: 18px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--accent, var(--ok)) 16%, transparent);
  color: var(--muted);
}

.small {
  padding: 3px 10px;
  font-size: 12px;
}

.danger {
  color: var(--danger);
}

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
}
</style>
