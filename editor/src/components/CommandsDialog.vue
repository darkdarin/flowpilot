<script setup lang="ts">
// Quick commands people taught the assistant: phrases that Quick commands runs at once, without
// an LLM, and what each does in Home Assistant. Phrases and the reply can be edited.
import { computed, onMounted, ref } from "vue";
import type { CommandList, QuickCommand } from "../api";
import { t } from "../i18n";
import { api, commandsOpen, flash } from "../store";

const data = ref<CommandList>({ commands: [], people: [] });
const busy = ref(false);
const editing = ref<string | null>(null);
const editPhrases = ref("");
const editReply = ref("");

const names = computed(() => new Map(data.value.people.map((p) => [p.id, p.name])));

async function run(action: () => Promise<unknown>) {
  busy.value = true;
  try {
    await action();
    data.value = await api.value!.commands();
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = false;
  }
}

onMounted(() => run(async () => undefined));

/** "intent__HassTurnOn" → "HassTurnOn"; arguments in short */
function action(a: QuickCommand["actions"][number]): string {
  const tool = a.tool.split("__").pop() ?? a.tool;
  const args = Object.entries(a.arguments)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : String(v)}`)
    .join("; ");
  return args ? `${tool} (${args})` : tool;
}

function who(c: QuickCommand): string {
  const by = c.created_by ? (names.value.get(c.created_by) ?? c.created_by) : "";
  const when = new Date(c.created_at).toLocaleDateString();
  return [by, when, t("commands.uses", { n: c.uses })].filter(Boolean).join(" · ");
}

function startEdit(c: QuickCommand) {
  editing.value = c.id;
  editPhrases.value = c.phrases.join("\n");
  editReply.value = c.reply;
}

function saveEdit(id: string) {
  const phrases = editPhrases.value.split("\n").map((p) => p.trim()).filter(Boolean);
  if (!phrases.length) return;
  void run(async () => {
    await api.value!.updateCommand(id, phrases, editReply.value.trim());
    editing.value = null;
  });
}

function remove(c: QuickCommand) {
  if (window.confirm(t("commands.confirmRemove", { text: c.phrases[0] }))) {
    void run(() => api.value!.deleteCommand(c.id));
  }
}
</script>

<template>
  <div class="modal" @click.self="commandsOpen = false" @keydown.esc="commandsOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('commands.title')">
      <h2>{{ t("commands.title") }}</h2>
      <p class="hint">{{ t("commands.hint") }}</p>

      <ul v-if="data.commands.length" class="list">
        <li v-for="c in data.commands" :key="c.id">
          <form v-if="editing === c.id" class="edit" @submit.prevent="saveEdit(c.id)">
            <label class="lbl">{{ t("commands.phrases") }}</label>
            <textarea v-model="editPhrases" class="field" rows="3" />
            <label class="lbl">{{ t("commands.reply") }}</label>
            <input v-model="editReply" class="field" maxlength="200" :placeholder="t('commands.replyDefault')" />
            <div class="row end">
              <button type="button" class="btn ghost small" @click="editing = null">{{ t("graph.cancel") }}</button>
              <button type="submit" class="btn primary small" :disabled="!editPhrases.trim() || busy">{{ t("memory.save") }}</button>
            </div>
          </form>
          <template v-else>
            <div class="row">
              <span class="phrases">
                <b v-for="(p, i) in c.phrases" :key="i">«{{ p }}»</b>
              </span>
              <button type="button" class="btn ghost small" @click="startEdit(c)">{{ t("memory.edit") }}</button>
              <button type="button" class="btn ghost small danger" :disabled="busy" @click="remove(c)">{{ t("memory.remove") }}</button>
            </div>
            <ol class="actions-list">
              <li v-for="(a, i) in c.actions" :key="i"><code>{{ action(a) }}</code></li>
            </ol>
            <p class="meta">
              <span v-if="c.reply">{{ t("commands.answers", { text: c.reply }) }} · </span>{{ who(c) }}
            </p>
          </template>
        </li>
      </ul>
      <p v-else class="hint">{{ t("commands.none") }}</p>

      <div class="actions">
        <button type="button" class="btn ghost" @click="commandsOpen = false">{{ t("secrets.close") }}</button>
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
  width: min(680px, calc(100% - 32px));
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
  gap: 8px;
}

.list > li {
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-raised);
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.row.end {
  justify-content: flex-end;
}

.phrases {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
}

.phrases b {
  font-weight: 600;
}

.actions-list {
  margin: 6px 0 0;
  padding-left: 20px;
  font-size: 12px;
  color: var(--muted);
}

.meta {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--faint);
}

.edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lbl {
  font-size: 12px;
  color: var(--muted);
}

.danger {
  color: var(--danger);
}

.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
