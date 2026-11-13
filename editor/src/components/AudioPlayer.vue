<script setup lang="ts">
// A recording to play and download (live debug: the Data tab and the journal).
import { t } from "../i18n";
import { download, play, playing } from "../playback";

defineProps<{ file: string; seconds?: number }>();
</script>

<template>
  <div class="audio">
    <button
      class="play"
      :class="{ on: playing === file }"
      :title="playing === file ? t('debug.stopPlaying') : t('debug.play')"
      :aria-label="playing === file ? t('debug.stopPlaying') : t('debug.play')"
      @click="play(file)"
    >
      <svg v-if="playing === file" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
      <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" /></svg>
    </button>
    <span v-if="seconds !== undefined" class="secs">{{ seconds.toFixed(1) }} s</span>
    <button class="icon" :title="t('debug.download')" :aria-label="t('debug.download')" @click="download(file)">
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10" /></svg>
    </button>
  </div>
</template>

<style scoped>
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
