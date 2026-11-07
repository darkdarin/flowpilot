<script setup lang="ts">
// Skills: tools the owner describes for the LLM agent on top of Home Assistant's actions. The
// model only fills the fields; text goes into an action escaped ({query|url}, {query|shell}).
import { onMounted, ref } from "vue";
import type { Skill, SkillField } from "../api";
import { t } from "../i18n";
import { api, flash, skillsOpen } from "../store";

interface Draft {
  id?: string;
  name: string;
  description: string;
  enabled: boolean;
  fields: (SkillField & { optionsText: string })[];
  actions: { action: string; target: string; data: string }[];
}

const skills = ref<Skill[]>([]);
const busy = ref(false);
const draft = ref<Draft | null>(null);

async function load() {
  skills.value = (await api.value!.skills()).skills;
}

onMounted(() => void load().catch((err) => flash((err as Error).message, "error")));

function edit(skill?: Skill) {
  draft.value = skill
    ? {
        id: skill.id,
        name: skill.name,
        description: skill.description,
        enabled: skill.enabled,
        fields: skill.fields.map((f) => ({ ...f, optionsText: f.options.join(", ") })),
        actions: skill.actions.map((a) => ({
          action: a.action,
          target: Object.keys(a.target).length ? JSON.stringify(a.target) : "",
          data: JSON.stringify(a.data, null, 2),
        })),
      }
    : {
        name: "",
        description: "",
        enabled: true,
        fields: [],
        actions: [{ action: "", target: "", data: "{}" }],
      };
}

function addField() {
  draft.value?.fields.push({ name: "", title: "", type: "text", options: [], optionsText: "", required: true });
}

function json(text: string, what: string): Record<string, unknown> {
  if (!text.trim()) return {};
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    /* reported below */
  }
  throw new Error(t("skills.badJson", { what }));
}

async function save() {
  const d = draft.value;
  if (!d) return;
  busy.value = true;
  try {
    await api.value!.saveSkill({
      id: d.id,
      name: d.name.trim(),
      description: d.description.trim(),
      enabled: d.enabled,
      fields: d.fields.map((f) => ({
        name: f.name.trim(),
        title: f.title.trim(),
        type: f.type,
        required: f.required,
        options: f.type === "choice" ? f.optionsText.split(",").map((o) => o.trim()).filter(Boolean) : [],
      })),
      actions: d.actions.map((a, i) => ({
        action: a.action.trim(),
        target: json(a.target, t("skills.target") + ` #${i + 1}`),
        data: json(a.data, t("skills.data") + ` #${i + 1}`),
      })),
    });
    draft.value = null;
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = false;
  }
}

async function toggle(skill: Skill) {
  try {
    await api.value!.saveSkill({ ...skill, enabled: !skill.enabled });
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

async function remove(skill: Skill) {
  if (!window.confirm(t("skills.confirmRemove", { name: skill.name }))) return;
  await api.value!.deleteSkill(skill.id);
  await load();
}
</script>

<template>
  <div class="modal" @click.self="skillsOpen = false" @keydown.esc="skillsOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('skills.title')">
      <h2>{{ t("skills.title") }}</h2>
      <p class="hint">{{ t("skills.hint") }}</p>

      <form v-if="draft" class="form" @submit.prevent="save">
        <label class="lbl">{{ t("skills.name") }}</label>
        <input v-model="draft.name" class="field code" placeholder="tv_open_app" />
        <label class="lbl">{{ t("skills.description") }}</label>
        <textarea v-model="draft.description" class="field" rows="3" :placeholder="t('skills.descriptionHint')" />

        <h3>{{ t("skills.fields") }}</h3>
        <div v-for="(f, i) in draft.fields" :key="i" class="field-row">
          <input v-model="f.name" class="field code small" placeholder="app" />
          <input v-model="f.title" class="field" :placeholder="t('skills.fieldTitle')" />
          <select v-model="f.type" class="field small">
            <option value="text">{{ t("skills.text") }}</option>
            <option value="number">{{ t("skills.number") }}</option>
            <option value="choice">{{ t("skills.choice") }}</option>
          </select>
          <input v-if="f.type === 'choice'" v-model="f.optionsText" class="field" :placeholder="t('skills.options')" />
          <label class="check"><input v-model="f.required" type="checkbox" />{{ t("skills.required") }}</label>
          <button type="button" class="btn ghost small danger" @click="draft.fields.splice(i, 1)">×</button>
        </div>
        <button type="button" class="btn ghost small" @click="addField">{{ t("skills.addField") }}</button>

        <h3>{{ t("skills.actions") }}</h3>
        <p class="hint">{{ t("skills.actionsHint") }}</p>
        <div v-for="(a, i) in draft.actions" :key="i" class="action">
          <div class="field-row">
            <input v-model="a.action" class="field code" placeholder="androidtv.adb_command" />
            <input v-model="a.target" class="field code" placeholder='{"entity_id": "media_player.tv"}' />
            <button type="button" class="btn ghost small danger" :disabled="draft.actions.length < 2" @click="draft.actions.splice(i, 1)">×</button>
          </div>
          <textarea v-model="a.data" class="field code" rows="3" placeholder='{"command": "monkey -p {app} 1"}' />
        </div>
        <button type="button" class="btn ghost small" @click="draft.actions.push({ action: '', target: '', data: '{}' })">{{ t("skills.addAction") }}</button>

        <div class="row end">
          <button type="button" class="btn ghost" @click="draft = null">{{ t("graph.cancel") }}</button>
          <button type="submit" class="btn primary" :disabled="busy">{{ t("memory.save") }}</button>
        </div>
      </form>

      <template v-else>
        <ul v-if="skills.length" class="list">
          <li v-for="s in skills" :key="s.id" :class="{ off: !s.enabled }">
            <div class="row">
              <code class="name">{{ s.name }}</code>
              <span class="desc">{{ s.description }}</span>
              <label class="check"><input type="checkbox" :checked="s.enabled" @change="toggle(s)" />{{ t("skills.enabled") }}</label>
              <button type="button" class="btn ghost small" @click="edit(s)">{{ t("memory.edit") }}</button>
              <button type="button" class="btn ghost small danger" @click="remove(s)">{{ t("memory.remove") }}</button>
            </div>
            <p class="meta">
              <span v-for="f in s.fields" :key="f.name">{{ f.name }}<template v-if="f.type === 'choice'"> ({{ f.options.join(", ") }})</template> · </span>
              <code v-for="(a, i) in s.actions" :key="i">{{ a.action }} </code>
            </p>
          </li>
        </ul>
        <p v-else class="hint">{{ t("skills.none") }}</p>
        <div class="row end">
          <button type="button" class="btn ghost" @click="edit()">{{ t("skills.add") }}</button>
          <button type="button" class="btn ghost" @click="skillsOpen = false">{{ t("secrets.close") }}</button>
        </div>
      </template>
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
  width: min(760px, calc(100% - 32px));
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
  margin: 8px 0 0;
  font-size: 13px;
  font-weight: 600;
}

.hint {
  margin: 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
}

.form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.lbl {
  font-size: 12px;
  color: var(--muted);
}

.field-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.field-row .field {
  flex: 1;
  min-width: 120px;
}

.field-row .field.small {
  flex: 0 1 130px;
}

.action {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  border: 1px dashed var(--line);
  border-radius: 8px;
}

.code {
  font-family: var(--mono, monospace);
  font-size: 12px;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
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

.list > li.off {
  opacity: 0.6;
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

.name {
  font-weight: 600;
}

.desc {
  flex: 1;
  font-size: 12.5px;
}

.meta {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--faint);
}

.danger {
  color: var(--danger);
}
</style>
