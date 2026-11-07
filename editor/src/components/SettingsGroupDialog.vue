<script setup lang="ts">
// A group of a node's settings (x-flowpilot-group), edited in its own dialog: the inspector shows
// the group as one entry, so related settings (an agent's memory) do not crowd the panel.
import type { JsonSchema, NodeInfo } from "../api";
import { t } from "../i18n";
import { fieldDescription, fieldOptionTitles, fieldTitle, groupDescription, groupTitle } from "../texts";
import SchemaField from "./SchemaField.vue";

const props = defineProps<{
  spec: NodeInfo;
  group: string;
  fields: [string, JsonSchema][];
  required: Set<string>;
  nodeId: string;
  nodeType: string;
  config: Record<string, unknown>;
}>();
const emit = defineEmits<{ set: [key: string, value: unknown]; close: [] }>();
</script>

<template>
  <div class="modal" @click.self="emit('close')" @keydown.esc="emit('close')">
    <section class="dialog" role="dialog" aria-modal="true" :aria-label="groupTitle(props.spec, props.group)">
      <h2>{{ groupTitle(spec, group) }}</h2>
      <p v-if="groupDescription(spec, group)" class="hint">{{ groupDescription(spec, group) }}</p>
      <SchemaField
        v-for="[key, schema] in fields"
        :key="nodeId + key"
        :name="key"
        :schema="schema"
        :required="required.has(key)"
        :label="fieldTitle(spec, key)"
        :description="fieldDescription(spec, key)"
        :node-type="nodeType"
        :config="config"
        :option-titles="fieldOptionTitles(spec, key)"
        :model-value="config[key]"
        @update:model-value="emit('set', key, $event)"
      />
      <div class="actions">
        <button type="button" class="btn primary" autofocus @click="emit('close')">{{ t("inspector.groupClose") }}</button>
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
  width: min(520px, calc(100% - 32px));
  max-height: calc(100% - 64px);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
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

.hint {
  margin: -6px 0 0;
  color: var(--muted);
  font-size: 12.5px;
  line-height: 1.45;
}

.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
