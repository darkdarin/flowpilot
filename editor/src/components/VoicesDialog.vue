<script setup lang="ts">
// Voices: the household's voice profiles for the Who speaks node. A sample is a person speaking,
// from the microphone or from a turn a Debug probe recorded (the same microphone the satellite
// hears with: better). The runtime keeps embeddings of the samples, not the audio. "Check a voice"
// shows how close some speech is to each profile, to tune the threshold on your own voices.
import { computed, onMounted, ref } from "vue";
import type { HostOption, Recording, Voices, VoiceTest } from "../api";
import { micAvailable, startRecording, toWav } from "../audio";
import { t } from "../i18n";
import { play, playing } from "../playback";
import { api, flash, nodes, voicesOpen } from "../store";

const voices = ref<Voices | null>(null);
const busy = ref<string | null>(null); // what is being done: a person's id, "test", "model"
const recording = ref<string | null>(null); // whose voice the microphone records now
let stopRecording: (() => Promise<Blob>) | null = null;
const picking = ref<string | null>(null); // whose sample is picked from the recordings
const recordings = ref<Recording[]>([]);
const test = ref<VoiceTest | null>(null);
const threshold = ref(0);
const margin = ref(0);

const model = computed(() => voices.value?.models.find((m) => m.id === voices.value?.model));
// people of Home Assistant, and anyone with a profile Home Assistant no longer lists
const people = computed<HostOption[]>(() => {
  const listed = voices.value?.people ?? [];
  const extra = Object.keys(voices.value?.profiles ?? {})
    .filter((id) => !listed.some((p) => p.id === id))
    .map((id) => ({ id, name: id }));
  return [...listed, ...extra];
});
const nameOf = (id: string) => people.value.find((p) => p.id === id)?.name ?? id;

async function load() {
  voices.value = await api.value!.voices();
  threshold.value = voices.value.threshold;
  margin.value = voices.value.margin;
}

onMounted(() => void load().catch((err) => flash((err as Error).message, "error")));

async function run(what: string, job: () => Promise<unknown>) {
  busy.value = what;
  try {
    await job();
    await load();
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    busy.value = null;
  }
}

function setModel(id: string) {
  void run("model", () => api.value!.setVoices({ model: id }));
}

function downloadModel() {
  void run("model", () => api.value!.downloadVoiceModel());
}

function saveTuning() {
  void run("tuning", () => api.value!.setVoices({ threshold: threshold.value, margin: margin.value }));
}

function resetThreshold() {
  void run("tuning", () => api.value!.setVoices({ threshold: null }));
}

/** Record from the microphone; the second click stops and sends it to `done`. */
async function record(who: string, done: (wav: Blob) => Promise<unknown>) {
  if (recording.value === who && stopRecording) {
    const stop = stopRecording;
    stopRecording = null;
    recording.value = null;
    const wav = await toWav(await stop());
    await run(who, () => done(wav));
    return;
  }
  try {
    stopRecording = (await startRecording()).stop;
    recording.value = who;
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

function recordSample(user: string) {
  void record(user, (wav) => api.value!.addVoiceSample(user, wav));
}

function recordTest() {
  void record("test", async (wav) => (test.value = await api.value!.testVoice(wav)));
}

async function pickFrom(user: string) {
  if (picking.value === user) {
    picking.value = null;
    return;
  }
  try {
    // what people said: the client's audio (or a Debug probe's recording), not speech it made
    const source = new Set(nodes.value.filter((n) => n.data?.type === "client.source").map((n) => n.id));
    recordings.value = (await api.value!.recordings()).recordings.filter((r) => r.port === null || source.has(r.node));
    picking.value = user;
  } catch (err) {
    flash((err as Error).message, "error");
  }
}

function useRecording(user: string, file: string) {
  picking.value = null;
  void run(user, () => api.value!.addVoiceSample(user, { recording: file }));
}

function testRecording(file: string) {
  picking.value = null;
  void run("test", async () => (test.value = await api.value!.testVoice({ recording: file })));
}

function removeSample(user: string, id: string) {
  void run(user, () => api.value!.deleteVoiceSample(user, id));
}

function removeVoice(user: string) {
  if (!confirm(t("voices.confirmRemove", { name: nameOf(user) }))) return;
  void run(user, () => api.value!.deleteVoice(user));
}

const fmtTime = (iso: string) => new Date(iso).toLocaleString();

// why a voice was not matched, as the runtime says it, in the UI language
const REASONS: Record<string, "voices.notClose" | "voices.tooClose"> = {
  "not close enough to anyone": "voices.notClose",
  "too close to two people": "voices.tooClose",
};
const reasonText = (reason: string) => (REASONS[reason] ? t(REASONS[reason]) : reason);
</script>

<template>
  <div class="modal" @click.self="voicesOpen = false" @keydown.esc="voicesOpen = false">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="t('voices.title')">
      <h2>{{ t("voices.title") }}</h2>
      <p class="hint">{{ t("voices.hint") }}</p>

      <template v-if="voices">
        <div class="settings">
          <label class="lbl" for="voice-model">{{ t("voices.model") }}</label>
          <div class="row">
            <select id="voice-model" class="field" :value="voices.model" :disabled="!!busy" @change="setModel(($event.target as HTMLSelectElement).value)">
              <option v-for="m in voices.models" :key="m.id" :value="m.id">{{ m.title }}{{ m.ready ? "" : ` — ${t("voices.notDownloaded")}` }}</option>
            </select>
            <button v-if="model && !model.ready" class="btn" :disabled="!!busy" @click="downloadModel">
              {{ busy === "model" ? t("voices.downloading") : t("voices.download") }}
            </button>
          </div>
          <div class="tuning">
            <label class="lbl" for="voice-threshold">{{ t("voices.threshold") }}</label>
            <input id="voice-threshold" v-model.number="threshold" class="field num" type="number" min="-1" max="1" step="0.01" @change="saveTuning" />
            <button v-if="voices.threshold !== voices.default_threshold" class="link" @click="resetThreshold">
              {{ t("voices.byDefault", { value: voices.default_threshold }) }}
            </button>
            <label class="lbl" for="voice-margin">{{ t("voices.margin") }}</label>
            <input id="voice-margin" v-model.number="margin" class="field num" type="number" min="0" max="1" step="0.01" @change="saveTuning" />
          </div>
          <p class="hint">{{ t("voices.tuningHint") }}</p>
        </div>

        <div class="check">
          <button class="btn" :class="{ live: recording === 'test' }" :disabled="!micAvailable || (!!busy && busy !== 'test') || (!!recording && recording !== 'test')" @click="recordTest">
            {{ recording === "test" ? t("voices.stop") : t("voices.check") }}
          </button>
          <div v-if="test" class="result">
            <p>
              <b>{{ test.match ? nameOf(test.match) : t("voices.unknown") }}</b>
              <span v-if="test.reason" class="muted"> · {{ reasonText(test.reason) }}</span>
            </p>
            <div v-for="(score, id) in test.scores" :key="id" class="bar">
              <span class="who">{{ nameOf(String(id)) }}</span>
              <span class="track"><span class="fill" :class="{ ok: score >= test.threshold }" :style="{ width: `${Math.max(0, score) * 100}%` }" /><span class="mark" :style="{ left: `${Math.max(0, test.threshold) * 100}%` }" /></span>
              <code>{{ score.toFixed(2) }}</code>
            </div>
          </div>
        </div>

        <ul class="people">
          <li v-for="p in people" :key="p.id" class="person">
            <div class="head">
              <b>{{ p.name }}</b>
              <span class="muted">{{ t("voices.samples", { n: (voices.profiles[p.id] ?? []).filter((s) => s.current).length }) }}</span>
              <span class="spacer" />
              <button class="btn small" :class="{ live: recording === p.id }" :disabled="!micAvailable || (!!busy && busy !== p.id) || (!!recording && recording !== p.id) || !model?.ready" @click="recordSample(p.id)">
                {{ recording === p.id ? t("voices.stop") : t("voices.record") }}
              </button>
              <button class="btn small" :disabled="!!busy || !!recording || !model?.ready" @click="pickFrom(p.id)">{{ t("voices.fromRecording") }}</button>
              <button v-if="voices.profiles[p.id]?.length" class="btn small danger" :disabled="!!busy" @click="removeVoice(p.id)">{{ t("voices.remove") }}</button>
            </div>
            <div v-if="voices.profiles[p.id]?.length" class="samples">
              <span v-for="s in voices.profiles[p.id]" :key="s.id" class="sample" :class="{ old: !s.current }" :title="`${fmtTime(s.created)} · ${s.level_db} dB${s.current ? '' : ' · ' + t('voices.otherModel')}`">
                {{ s.source === "recording" ? "🔊" : "🎙" }} {{ s.seconds }} s
                <button class="x" :aria-label="t('voices.removeSample')" :title="t('voices.removeSample')" @click="removeSample(p.id, s.id)">×</button>
              </span>
            </div>
            <div v-if="picking === p.id" class="recordings">
              <p v-if="!recordings.length" class="hint">{{ t("voices.noRecordings") }}</p>
              <div v-for="r in recordings.slice(0, 20)" :key="r.file" class="recording">
                <button class="play" :title="t('debug.play')" @click="play(r.file)">{{ playing === r.file ? "■" : "▶" }}</button>
                <span>{{ fmtTime(r.created) }} · {{ r.node }} · {{ r.seconds.toFixed(1) }} s</span>
                <span class="spacer" />
                <button class="btn small" @click="useRecording(p.id, r.file)">{{ t("voices.useRecording") }}</button>
                <button class="btn small" @click="testRecording(r.file)">{{ t("voices.checkRecording") }}</button>
              </div>
            </div>
          </li>
          <li v-if="!people.length" class="hint">{{ t("voices.noPeople") }}</li>
        </ul>
      </template>

      <div class="actions">
        <button class="btn" @click="voicesOpen = false">{{ t("secrets.close") }}</button>
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
  width: min(760px, calc(100% - 32px));
  max-height: calc(100% - 64px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
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

.hint,
.muted {
  margin: 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
}

.lbl {
  font-size: 12px;
  color: var(--muted);
}

.settings,
.check {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
}

.row,
.tuning,
.head,
.recording {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.num {
  width: 90px;
}

.link {
  border: 0;
  background: none;
  color: var(--focus);
  font-size: 12px;
  cursor: pointer;
}

.check {
  align-items: flex-start;
}

.result {
  width: 100%;
}

.result p {
  margin: 4px 0 8px;
}

.bar {
  display: grid;
  grid-template-columns: 140px 1fr 48px;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}

.track {
  position: relative;
  height: 8px;
  border-radius: 4px;
  background: var(--line);
}

.fill {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: 4px;
  background: var(--muted);
}

.fill.ok {
  background: var(--ok);
}

.mark {
  position: absolute;
  top: -3px;
  bottom: -3px;
  width: 2px;
  background: var(--text);
}

.people {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.person {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel-raised);
}

.spacer {
  flex: 1;
}

.samples {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.sample {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 4px 1px 8px;
  border: 1px solid var(--line);
  border-radius: 11px;
  font-size: 12px;
}

.sample.old {
  opacity: 0.5;
}

.x {
  border: 0;
  background: none;
  color: var(--muted);
  cursor: pointer;
}

.recordings {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 220px;
  overflow-y: auto;
  font-size: 12.5px;
}

.play {
  width: 24px;
  height: 22px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: transparent;
  cursor: pointer;
}

.btn.small {
  padding: 2px 8px;
  font-size: 12px;
}

.btn.live {
  border-color: var(--danger);
  color: var(--danger);
}

.btn.danger {
  color: var(--danger);
}

.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
