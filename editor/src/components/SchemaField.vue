<script setup lang="ts">
// One config property rendered from its JSON Schema. Secrets ({"type": "secret"}) become a
// reference to the runtime secrets file: {"secret": "<name>"}.
import { computed, onBeforeUnmount, ref, watch } from "vue";
import type { HostOption, JsonSchema } from "../api";
import { languageName, t } from "../i18n";
import { hostOptions, nodeOptions, refreshOptions, secrets, secretsOpen } from "../store";
import NumberSlider from "./NumberSlider.vue";
import OptionPicker, { type PickerOption } from "./OptionPicker.vue";

const props = defineProps<{
  name: string;
  schema: JsonSchema;
  required: boolean;
  modelValue: unknown;
  label?: string; // localized title; the property name is shown next to it
  description?: string; // localized description
  nodeType?: string; // for choices the node lists itself
  config?: Record<string, unknown>; // the node's whole config (those choices depend on it)
  optionTitles?: Record<string, string>; // labels of enum values, localized
}>();
const emit = defineEmits<{ "update:modelValue": [value: unknown] }>();

const kind = computed(() => {
  const s = props.schema;
  if (s.enum) return "enum";
  const type = Array.isArray(s.type) ? s.type.find((x) => x !== "null") : s.type;
  if (type === "secret") return "secret";
  if (type === "boolean") return "boolean";
  if (type === "integer" || type === "number") return "number";
  if (type === "string") return "string";
  return "json";
});

const inputId = computed(() => `cfg-${props.name}`);

// Numbers with both bounds get a slider; the exclusive lower bound counts as the bound.
const range = computed(() => {
  const s = props.schema;
  const min = s.minimum ?? s.exclusiveMinimum;
  return kind.value === "number" && min !== undefined && s.maximum !== undefined
    ? { min, max: s.maximum }
    : null;
});
const numberValue = computed(() => (typeof props.modelValue === "number" ? props.modelValue : undefined));

const stringValue = computed(() => (props.modelValue === undefined ? "" : String(props.modelValue)));
const invalidPattern = computed(() => {
  const p = props.schema.pattern;
  return kind.value === "string" && p && stringValue.value !== "" && !new RegExp(p).test(stringValue.value);
});

function setString(value: string) {
  emit("update:modelValue", value === "" ? undefined : value);
}

function setNumber(value: string) {
  if (value.trim() === "") return emit("update:modelValue", undefined);
  const n = Number(value);
  if (!Number.isNaN(n)) emit("update:modelValue", props.schema.type === "integer" ? Math.trunc(n) : n);
}

// Settings that name something a host provides get its list; strict kinds warn about unknown ids.
const optionKind = computed(() => props.schema["x-flowpilot-options"]);
const choices = computed(() => (optionKind.value ? (hostOptions.value.options[optionKind.value] ?? []) : []));
const chosen = computed(() => choices.value.find((c) => c.id === stringValue.value));
const unknownRef = computed(
  () =>
    !!optionKind.value &&
    hostOptions.value.strict.includes(optionKind.value) &&
    stringValue.value !== "" &&
    !chosen.value,
);

// Choices the node lists itself from the rest of its config (e.g. a Wyoming service's models).
const fromNode = computed(() => !!props.schema["x-flowpilot-node-options"] && !!props.nodeType);
const dynamic = ref<{ options: HostOption[]; error?: string }>({ options: [] });
const dynamicLoading = ref(false);
let dynamicTimer: number | undefined;
let dynamicSeq = 0;

async function loadDynamic(): Promise<void> {
  if (!fromNode.value) return;
  const seq = ++dynamicSeq;
  const config = { ...(props.config ?? {}) };
  delete config[props.name];
  dynamicLoading.value = true;
  dynamic.value = { options: [] }; // the old list belongs to the old config
  const result = await nodeOptions(props.nodeType!, props.name, config);
  if (seq !== dynamicSeq) return;
  dynamic.value = result;
  dynamicLoading.value = false;
}

watch(
  () => {
    const config = { ...(props.config ?? {}) };
    delete config[props.name];
    return fromNode.value ? JSON.stringify([props.nodeType, config]) : "";
  },
  (key) => {
    window.clearTimeout(dynamicTimer);
    if (key) dynamicTimer = window.setTimeout(() => void loadDynamic(), 300);
  },
  { immediate: true },
);
onBeforeUnmount(() => window.clearTimeout(dynamicTimer));

// "Refreshing…" stays long enough to be seen even when the answer is instant, then "Refreshed".
const refreshing = ref(false);
const refreshed = ref(false);
let refreshedTimer: number | undefined;

async function refresh() {
  if (refreshing.value) return;
  refreshing.value = true;
  refreshed.value = false;
  window.clearTimeout(refreshedTimer);
  const minimum = new Promise((resolve) => window.setTimeout(resolve, 500));
  try {
    await Promise.all([refreshOptions(), loadDynamic(), minimum]);
  } finally {
    refreshing.value = false;
    refreshed.value = true;
    refreshedTimer = window.setTimeout(() => (refreshed.value = false), 1800);
  }
}
onBeforeUnmount(() => window.clearTimeout(refreshedTimer));

const isLanguage = computed(() => props.schema.format === "language");
const usePicker = computed(
  () => kind.value === "enum" || (kind.value === "string" && (!!optionKind.value || fromNode.value)),
);
const pickerOptions = computed<PickerOption[]>(() => {
  let list: PickerOption[] =
    kind.value === "enum"
      ? (props.schema.enum ?? []).map((v) => {
          const label = props.optionTitles?.[String(v)];
          return label ? { id: String(v), name: label, description: String(v) } : { id: String(v) };
        })
      : optionKind.value
        ? choices.value
        : dynamic.value.options;
  if (isLanguage.value) {
    list = list
      .map((o) => ({ id: o.id, name: languageName(o.id), description: o.id }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }
  return list;
});
const emptyLabel = computed(() => {
  if (props.required) return undefined;
  const fallback = props.schema.default;
  if (fallback === undefined) return t("picker.default");
  return `${t("picker.default")}: ${props.optionTitles?.[String(fallback)] ?? String(fallback)}`;
});

const secretName = computed(() => {
  const v = props.modelValue as { secret?: string } | undefined;
  return v && typeof v === "object" ? (v.secret ?? "") : "";
});
const secretSet = computed(() => secrets.value.some((s) => s.name === secretName.value && s.set));
const secretOptions = computed<PickerOption[]>(() =>
  secrets.value.map((s) => ({ id: s.name, description: s.set ? t("secrets.set") : t("secrets.unset") })),
);

const jsonText = ref("");
const jsonBad = ref(false);
watch(
  () => props.modelValue,
  (v) => {
    if (kind.value === "json") jsonText.value = v === undefined ? "" : JSON.stringify(v, null, 2);
  },
  { immediate: true },
);

function setJson(text: string) {
  jsonText.value = text;
  if (text.trim() === "") {
    jsonBad.value = false;
    return emit("update:modelValue", undefined);
  }
  try {
    emit("update:modelValue", JSON.parse(text));
    jsonBad.value = false;
  } catch {
    jsonBad.value = true;
  }
}

const hint = computed(() => {
  const s = props.schema;
  const parts: string[] = [];
  if (s.pattern) parts.push(s.pattern);
  if (s.minimum !== undefined) parts.push(`≥ ${s.minimum}`);
  if (s.exclusiveMinimum !== undefined) parts.push(`> ${s.exclusiveMinimum}`);
  if (s.maximum !== undefined) parts.push(`≤ ${s.maximum}`);
  return parts.join(" · ");
});
</script>

<template>
  <div class="schema-field">
    <label :for="inputId">
      <span class="title">{{ label || name }}</span>
      <span v-if="required" class="req" :title="t('inspector.required')">*</span>
      <code v-if="label" class="key">{{ name }}</code>
    </label>
    <p v-if="description" class="desc">{{ description }}</p>

    <template v-if="usePicker">
      <OptionPicker
        :input-id="inputId"
        :model-value="stringValue"
        :options="pickerOptions"
        :empty-label="emptyLabel"
        :allow-custom="kind !== 'enum'"
        :loading="fromNode && dynamicLoading"
        :error="fromNode ? dynamic.error : undefined"
        :warn="unknownRef"
        :invalid="!!invalidPattern"
        @update:model-value="setString"
      />
      <small v-if="unknownRef" class="warn">{{ t("form.notFound") }}</small>
      <small v-else-if="optionKind && !hostOptions.available">{{ t("form.noHost") }}</small>
      <button v-if="optionKind || fromNode" type="button" class="refresh" :disabled="refreshing" :aria-busy="refreshing" @click="refresh">
        <svg v-if="refreshing" class="spin" viewBox="0 0 16 16" aria-hidden="true"><path d="M14 8a6 6 0 1 1-6-6" /></svg>
        <svg v-else-if="refreshed" class="done" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5 6.5 12 13 4.5" /></svg>
        {{ refreshing ? t("form.refreshing") : refreshed ? t("form.refreshed") : t("form.refresh") }}
      </button>
    </template>

    <label v-else-if="kind === 'boolean'" class="check">
      <input :id="inputId" type="checkbox" :checked="modelValue === true" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      <span>{{ modelValue === undefined ? t("form.unset") : String(modelValue) }}</span>
    </label>

    <NumberSlider
      v-else-if="range"
      :input-id="inputId"
      :model-value="numberValue"
      :min="range.min"
      :max="range.max"
      :integer="schema.type === 'integer'"
      :fallback="typeof schema.default === 'number' ? schema.default : undefined"
      @update:model-value="(v: number | undefined) => emit('update:modelValue', v)"
    />

    <input
      v-else-if="kind === 'number'"
      :id="inputId"
      class="field"
      type="number"
      :step="schema.type === 'integer' ? 1 : 'any'"
      :value="stringValue"
      :placeholder="schema.default !== undefined ? String(schema.default) : ''"
      @input="setNumber(($event.target as HTMLInputElement).value)"
    />

    <template v-else-if="kind === 'secret'">
      <OptionPicker
        :input-id="inputId"
        :model-value="secretName"
        :options="secretOptions"
        :empty-label="required ? undefined : t('form.unset')"
        :allow-custom="true"
        :warn="!!secretName && !secretSet"
        @update:model-value="(v: string) => emit('update:modelValue', v ? { secret: v } : undefined)"
      />
      <small v-if="secretName && !secretSet" class="warn">{{ t("form.secretUnset") }}</small>
      <small v-else>{{ t("form.secretHint") }}</small>
      <button type="button" class="refresh" @click="secretsOpen = true">{{ t("form.manageSecrets") }}</button>
    </template>


    <textarea
      v-else-if="kind === 'string' && schema.format === 'multiline'"
      :id="inputId"
      class="field multiline"
      rows="6"
      :value="stringValue"
      :placeholder="schema.default !== undefined ? String(schema.default) : ''"
      @input="setString(($event.target as HTMLTextAreaElement).value)"
    />

    <input
      v-else-if="kind === 'string'"
      :id="inputId"
      class="field"
      :class="{ invalid: invalidPattern }"
      :value="stringValue"
      :placeholder="schema.default !== undefined ? String(schema.default) : ''"
      @input="setString(($event.target as HTMLInputElement).value)"
    />

    <template v-else>
      <textarea :id="inputId" class="field code" rows="4" :class="{ invalid: jsonBad }" :value="jsonText" @input="setJson(($event.target as HTMLTextAreaElement).value)" />
      <small v-if="jsonBad" class="bad">{{ t("form.invalidJson") }}</small>
    </template>

    <small v-if="isLanguage && !usePicker && stringValue">{{ languageName(stringValue) }}</small>
    <small v-if="hint && kind !== 'secret' && !usePicker">{{ hint }}</small>
  </div>
</template>

<style scoped>
.schema-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

label {
  display: flex;
  gap: 3px;
  align-items: baseline;
}

label .title {
  font-weight: 500;
}

label .key {
  margin-left: auto;
  color: var(--faint);
  font-size: 11px;
}

.desc {
  margin: -2px 0 2px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
}

.req {
  color: var(--danger);
}

small {
  color: var(--faint);
  font-size: 11.5px;
  overflow-wrap: anywhere;
}

small.bad {
  color: var(--danger);
}

small.warn {
  color: var(--warn);
}

small.chosen {
  color: var(--muted);
}

.field.warn {
  border-color: var(--warn);
}

.refresh {
  align-self: flex-start;
  padding: 0;
  border: 0;
  background: none;
  color: var(--focus);
  font-size: 11.5px;
  cursor: pointer;
}

.refresh {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.refresh:hover:not(:disabled) {
  text-decoration: underline;
}

.refresh:disabled {
  color: var(--muted);
  cursor: progress;
}

.done {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: var(--ok);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.spin {
  width: 11px;
  height: 11px;
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.multiline {
  resize: vertical;
  min-height: 90px;
  font-size: 12.5px;
  line-height: 1.45;
}

.check {
  gap: 8px;
  align-items: center;
  color: var(--muted);
}
</style>
