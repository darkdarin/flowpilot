<script setup lang="ts">
// Runtime secrets (API keys): set, replace or remove them. Values are write-only: the runtime never
// sends one back, the list only says whether a secret is set and which graphs use it.
import { computed, ref } from "vue";
import { t } from "../i18n";
import { changeSecret, flash, secrets, secretsOpen } from "../store";

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

const editing = ref<string | null>(null); // the secret whose value is being typed
const value = ref("");
const newName = ref("");
const newValue = ref("");
const busy = ref(false);

const nameInvalid = computed(() => newName.value !== "" && !NAME_RE.test(newName.value));
const nameTaken = computed(() => secrets.value.some((s) => s.name === newName.value && s.set));

async function save(name: string, secret: string | null) {
  busy.value = true;
  try {
    const failed = await changeSecret(name, secret);
    const problems = Object.entries(failed);
    if (problems.length) {
      flash(problems.map(([g, err]) => `${g}: ${err}`).join("\n"), "error");
    } else {
      flash(secret === null ? t("secrets.removed") : t("secrets.saved"));
    }
    editing.value = null;
    value.value = "";
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = false;
  }
}

async function add() {
  if (!newName.value || nameInvalid.value || !newValue.value) return;
  await save(newName.value, newValue.value);
  newName.value = "";
  newValue.value = "";
}

function remove(name: string) {
  if (window.confirm(t("secrets.confirmRemove", { name }))) void save(name, null);
}
</script>

<template>
  <div class="modal" @click.self="secretsOpen = false" @keydown.esc="secretsOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('secrets.title')">
      <h2>{{ t("secrets.title") }}</h2>
      <p class="hint">{{ t("secrets.hint") }}</p>

      <ul v-if="secrets.length" class="list">
        <li v-for="s in secrets" :key="s.name">
          <div class="row">
            <code class="name">{{ s.name }}</code>
            <span class="state" :class="{ set: s.set }">{{ s.set ? t("secrets.set") : t("secrets.unset") }}</span>
            <span v-if="s.graphs.length" class="used">{{ t("secrets.usedBy", { graphs: s.graphs.join(", ") }) }}</span>
            <span class="spacer" />
            <button v-if="editing !== s.name" type="button" class="btn ghost small" @click="(editing = s.name), (value = '')">
              {{ s.set ? t("secrets.replace") : t("secrets.setValue") }}
            </button>
            <button v-if="s.set" type="button" class="btn ghost small danger" :disabled="busy" @click="remove(s.name)">
              {{ t("secrets.remove") }}
            </button>
          </div>
          <form v-if="editing === s.name" class="edit" @submit.prevent="save(s.name, value)">
            <input v-model="value" class="field code" type="password" autocomplete="off" :placeholder="t('secrets.value')" autofocus />
            <button type="button" class="btn ghost small" @click="editing = null">{{ t("graph.cancel") }}</button>
            <button type="submit" class="btn primary small" :disabled="!value || busy">{{ t("secrets.save") }}</button>
          </form>
        </li>
      </ul>
      <p v-else class="hint">{{ t("secrets.none") }}</p>

      <form class="add" @submit.prevent="add">
        <h3>{{ t("secrets.add") }}</h3>
        <input v-model.trim="newName" class="field code" :class="{ invalid: nameInvalid }" :placeholder="t('secrets.name')" autocomplete="off" />
        <input v-model="newValue" class="field code" type="password" autocomplete="off" :placeholder="t('secrets.value')" />
        <button type="submit" class="btn primary small" :disabled="!newName || nameInvalid || !newValue || busy">
          {{ nameTaken ? t("secrets.replace") : t("secrets.save") }}
        </button>
        <small v-if="nameInvalid" class="bad">{{ t("secrets.badName") }}</small>
      </form>

      <div class="actions">
        <button type="button" class="btn ghost" @click="secretsOpen = false">{{ t("secrets.close") }}</button>
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
  width: min(560px, calc(100% - 32px));
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
  width: 100%;
  margin: 6px 0 0;
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
  padding: 8px 10px;
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

.edit {
  margin-top: 8px;
}

.edit .field,
.add .field {
  flex: 1;
  min-width: 140px;
}

.name {
  font-weight: 600;
}

.state {
  font-size: 11.5px;
  padding: 0 7px;
  line-height: 18px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--warn) 18%, transparent);
  color: var(--warn);
}

.state.set {
  background: color-mix(in srgb, var(--ok) 16%, transparent);
  color: var(--ok);
}

.used {
  font-size: 12px;
  color: var(--muted);
}

.spacer {
  flex: 1;
}

.small {
  padding: 3px 10px;
  font-size: 12px;
}

.danger {
  color: var(--danger);
}

.bad {
  width: 100%;
  color: var(--danger);
}

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
}
</style>
