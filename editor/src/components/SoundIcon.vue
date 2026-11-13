<script setup lang="ts">
// Marks something that carries sound (audio.pcm/1): a speaker.
import { t } from "../i18n";
</script>

<template>
  <svg class="sound" viewBox="0 0 16 16" role="img" :aria-label="t('data.sound')">
    <title>{{ t("data.sound") }}</title>
    <path d="M2.5 6h2.5L8.5 3v10L5 10H2.5z" class="body" />
    <path d="M10.5 5.5a3.5 3.5 0 0 1 0 5M12.3 3.8a6 6 0 0 1 0 8.4" class="waves" />
  </svg>
</template>

<style scoped>
.sound {
  width: 13px;
  height: 13px;
  flex: none;
  vertical-align: -2px;
  color: var(--cat-speech, var(--ok));
}

.body {
  fill: currentColor;
}

.waves {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.3;
  stroke-linecap: round;
}
</style>
