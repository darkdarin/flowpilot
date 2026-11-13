<script setup lang="ts">
// What one output passed on in a watched turn: audio to play and download, text, or data as a
// JSON tree. Shared by the journal and the Data tab.
import { t } from "../i18n";
import { download, play, playing } from "../playback";
import type { OutputData } from "../store";
import JsonBlock from "./JsonBlock.vue";

defineProps<{ out: OutputData }>();
</script>

<template>
  <div class="value">
    <p v-if="out.same" class="hint">{{ t("journal.same", { from: out.same }) }}</p>
    <p v-if="out.error" class="err">{{ out.error }}</p>
    <div v-if="out.type === 'audio.pcm/1' && out.file" class="audio">
      <button
        class="play"
        :class="{ on: playing === out.file }"
        :title="playing === out.file ? t('debug.stopPlaying') : t('debug.play')"
        :aria-label="playing === out.file ? t('debug.stopPlaying') : t('debug.play')"
        @click="play(out.file!)"
      >
        <svg v-if="playing === out.file" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
        <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" /></svg>
      </button>
      <span class="secs">{{ out.seconds?.toFixed(1) }} s</span>
      <button class="icon" :title="t('debug.download')" :aria-label="t('debug.download')" @click="download(out.file!)">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10" /></svg>
      </button>
    </div>
    <template v-else-if="out.text !== undefined">
      <pre class="plain">{{ out.text || "—" }}</pre>
      <p v-if="out.partials" class="hint">{{ t("journal.partials", { n: out.partials }) }}</p>
    </template>
    <JsonBlock v-else-if="out.json !== undefined" :text="out.json" />
    <p v-if="out.more" class="hint">{{ t("journal.more", { n: out.more }) }}</p>
  </div>
</template>

<style scoped>
.value {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: stretch;
}

.plain {
  margin: 0;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

.hint {
  margin: 0;
  font-size: 11px;
  color: var(--faint);
}

.err {
  margin: 0;
  font-size: 11.5px;
  color: var(--danger);
}

.audio {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px 3px 3px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--panel-raised);
}

.audio button {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.audio button:hover {
  color: var(--text);
  background: var(--line);
}

.audio .play {
  background: color-mix(in srgb, var(--cat-speech, var(--ok)) 25%, transparent);
  color: var(--text);
}

.audio .play.on {
  background: var(--cat-speech, var(--ok));
  color: var(--primary-text);
}

.audio svg {
  width: 13px;
  height: 13px;
}

.audio .play svg {
  fill: currentColor;
}

.audio .icon svg {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.secs {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
