<script setup lang="ts">
import { ref } from "vue";
import { t } from "../i18n";
import { connect } from "../store";

const value = ref("");
const bad = ref(false);
const busy = ref(false);
const error = ref("");

async function submit() {
  busy.value = true;
  error.value = "";
  try {
    bad.value = !(await connect(value.value.trim()));
  } catch (err) {
    error.value = t("error.load", { msg: (err as Error).message });
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="login">
    <form @submit.prevent="submit">
      <svg viewBox="0 0 120 40" class="mark" aria-hidden="true">
        <path d="M18 20 C 50 20, 60 8, 100 8" stroke="var(--type-audio)" />
        <path d="M18 20 C 50 20, 60 32, 100 32" stroke="var(--type-response)" />
        <circle cx="16" cy="20" r="6" fill="var(--type-audio)" />
        <rect x="96" y="4" width="8" height="8" rx="1.5" transform="rotate(45 100 8)" fill="var(--type-transcript)" />
        <circle cx="102" cy="32" r="6" fill="var(--type-response)" />
      </svg>
      <h1>{{ t("login.title") }}</h1>
      <p>{{ t("login.hint") }}</p>
      <label for="token">{{ t("login.token") }}</label>
      <input id="token" v-model="value" class="field code" type="password" autocomplete="current-password" autofocus />
      <p v-if="bad" class="bad">{{ t("login.bad") }}</p>
      <p v-if="error" class="bad">{{ error }}</p>
      <button class="btn primary" type="submit" :disabled="!value.trim() || busy">{{ t("login.connect") }}</button>
    </form>
  </div>
</template>

<style scoped>
.login {
  display: grid;
  place-items: center;
  height: 100%;
  background: radial-gradient(var(--canvas-dot) 1px, transparent 1px) 0 0 / 22px 22px, var(--canvas);
}

form {
  width: min(380px, calc(100% - 32px));
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 28px;
  border-radius: 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}

.mark {
  width: 96px;
  fill: none;
}

.mark path {
  stroke-width: 3;
  fill: none;
  stroke-linecap: round;
}

h1 {
  margin: 4px 0 0;
  font-size: 20px;
  letter-spacing: -0.015em;
}

p {
  margin: 0 0 6px;
  color: var(--muted);
}

label {
  font-size: 12px;
  color: var(--muted);
}

.bad {
  color: var(--danger);
}

.btn {
  margin-top: 6px;
  justify-content: center;
  height: 34px;
}
</style>
