<script setup lang="ts">
// Text from the journal: JSON as a tree that folds (with Raw for the JSON itself), anything else
// as it is.
import { computed, ref } from "vue";
import { t } from "../i18n";
import JsonView from "./JsonView.vue";

const props = defineProps<{ text?: string; value?: unknown }>();

const parsed = computed<{ ok: boolean; value?: unknown }>(() => {
  if (props.value !== undefined) return { ok: true, value: props.value };
  const text = (props.text ?? "").trim();
  if (!text.startsWith("{") && !text.startsWith("[")) return { ok: false };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
});

const raw = ref(false);
const rawText = computed(() => JSON.stringify(parsed.value.value, null, 2));
</script>

<template>
  <div class="block">
    <template v-if="parsed.ok">
      <button class="raw" :class="{ on: raw }" :title="t('journal.rawHint')" @click="raw = !raw">
        {{ t("journal.raw") }}
      </button>
      <pre v-if="raw" class="text">{{ rawText }}</pre>
      <JsonView v-else :value="parsed.value" />
    </template>
    <pre v-else class="text">{{ text }}</pre>
  </div>
</template>

<style scoped>
.block {
  position: relative;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-raised);
  max-height: 420px;
  overflow: auto;
}

.text {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.raw {
  position: sticky;
  float: right;
  top: 0;
  margin-left: 8px;
  padding: 1px 7px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  color: var(--muted);
  font-size: 10.5px;
  cursor: pointer;
}

.raw.on {
  color: var(--text);
  border-color: var(--focus);
}
</style>
