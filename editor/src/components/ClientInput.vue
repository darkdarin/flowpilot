<script setup lang="ts">
// On the Client input node in live debug: start a turn with text, a microphone recording or an
// audio file, without a satellite. The turn runs the deployed graph through the runtime's turn API.
import { onBeforeUnmount, ref } from "vue";
import { micAvailable, startRecording, toWav } from "../audio";
import { t } from "../i18n";
import { flash, hasDraft, live, startTurn, turnStarting } from "../store";

const text = ref("");
const recording = ref<{ stop: () => Promise<Blob> } | null>(null);
const file = ref<HTMLInputElement | null>(null);

async function sendText() {
  if (!text.value.trim()) return;
  await startTurn({ text: text.value.trim() });
}

async function sendAudio(data: Blob) {
  let wav: Blob;
  try {
    wav = await toWav(data);
  } catch {
    flash(t("client.badAudio"), "error");
    return;
  }
  await startTurn(wav);
}

async function toggleRecording() {
  if (recording.value) {
    const rec = recording.value;
    recording.value = null;
    await sendAudio(await rec.stop());
    return;
  }
  try {
    recording.value = await startRecording();
  } catch {
    flash(t("client.micDenied"), "error");
  }
}

async function onFile(e: Event) {
  const input = e.target as HTMLInputElement;
  const picked = input.files?.[0];
  input.value = "";
  if (picked) await sendAudio(picked);
}

onBeforeUnmount(() => void recording.value?.stop());
</script>

<template>
  <div class="client nodrag nowheel" @keydown.stop>
    <form class="row" @submit.prevent="sendText">
      <input v-model="text" :placeholder="t('client.text')" :disabled="!live" :aria-label="t('client.text')" />
      <button
        type="submit"
        class="icon"
        :disabled="!live || turnStarting || !text.trim()"
        :title="live ? t('client.send') : t('node.notLive')"
        :aria-label="t('client.send')"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" class="fill" /></svg>
      </button>
      <button
        type="button"
        class="icon mic"
        :class="{ on: recording }"
        :disabled="!live || !micAvailable || (turnStarting && !recording)"
        :title="!micAvailable ? t('client.noMic') : recording ? t('client.stopRecording') : t('client.record')"
        :aria-label="recording ? t('client.stopRecording') : t('client.record')"
        @click="toggleRecording"
      >
        <svg v-if="recording" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" class="fill" /></svg>
        <svg v-else viewBox="0 0 16 16" aria-hidden="true">
          <rect x="6" y="2" width="4" height="8" rx="2" />
          <path d="M3.8 7.5a4.2 4.2 0 0 0 8.4 0M8 11.7V14" />
        </svg>
      </button>
      <button
        type="button"
        class="icon"
        :disabled="!live || turnStarting"
        :title="t('client.upload')"
        :aria-label="t('client.upload')"
        @click="file?.click()"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V5M4.5 8.5 8 5l3.5 3.5M3 2.5h10" /></svg>
      </button>
      <input ref="file" type="file" accept="audio/*" hidden @change="onFile" />
    </form>
    <p v-if="!live" class="hint">{{ t("node.notLive") }}</p>
    <p v-else-if="hasDraft" class="hint">{{ t("client.deployed") }}</p>
  </div>
</template>

<style scoped>
.client {
  padding: 8px 12px 10px;
  border-top: 1px solid var(--line);
  cursor: auto;
}

.row {
  display: flex;
  gap: 4px;
  align-items: center;
}

input {
  flex: 1;
  min-width: 0;
  height: 26px;
  padding: 0 8px;
  font-size: 12px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  color: var(--text);
}

.icon {
  display: grid;
  place-items: center;
  flex: none;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: color-mix(in srgb, var(--cat) 20%, transparent);
  color: var(--text);
  cursor: pointer;
}

.icon:disabled {
  opacity: 0.4;
  cursor: default;
}

.icon:not(:disabled):hover {
  background: color-mix(in srgb, var(--cat) 35%, transparent);
}

.mic.on {
  background: var(--danger);
  color: var(--primary-text);
  animation: pulse 1s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.6;
  }
}

svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

svg .fill {
  fill: currentColor;
  stroke: none;
}

.hint {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--muted);
}
</style>
