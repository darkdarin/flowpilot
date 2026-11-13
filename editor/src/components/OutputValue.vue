<script setup lang="ts">
// What one output passed on in a watched turn: audio to play and download, text, or data as a
// JSON tree. Shared by the journal and the Data tab.
import { t } from "../i18n";
import type { OutputData } from "../store";
import AudioPlayer from "./AudioPlayer.vue";
import JsonBlock from "./JsonBlock.vue";

defineProps<{ out: OutputData }>();
</script>

<template>
  <div class="value">
    <p v-if="out.same" class="hint">{{ t("journal.same", { from: out.same }) }}</p>
    <p v-if="out.error" class="err">{{ out.error }}</p>
    <AudioPlayer v-if="out.type === 'audio.pcm/1' && out.file" :file="out.file" :seconds="out.seconds" />
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

</style>
