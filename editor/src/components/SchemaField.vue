<script setup lang="ts">
// One config property rendered from its JSON Schema. Secrets ({"type": "secret"}) become a
// reference to the runtime secrets file: {"secret": "<name>"}.
import { computed, ref, watch } from "vue";
import type { JsonSchema } from "../api";
import { t } from "../i18n";
import { hostOptions, refreshOptions } from "../store";

const props = defineProps<{ name: string; schema: JsonSchema; required: boolean; modelValue: unknown }>();
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

const secretName = computed(() => {
  const v = props.modelValue as { secret?: string } | undefined;
  return v && typeof v === "object" ? (v.secret ?? "") : "";
});

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
  if (s.description) parts.push(s.description);
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
      <code>{{ name }}</code>
      <span v-if="required" class="req" :title="t('inspector.required')">*</span>
    </label>

    <select v-if="kind === 'enum'" :id="inputId" class="field" :value="stringValue" @change="setString(($event.target as HTMLSelectElement).value)">
      <option value="">{{ schema.default !== undefined ? String(schema.default) : t("form.unset") }}</option>
      <option v-for="opt in schema.enum" :key="String(opt)" :value="String(opt)">{{ opt }}</option>
    </select>

    <label v-else-if="kind === 'boolean'" class="check">
      <input :id="inputId" type="checkbox" :checked="modelValue === true" @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />
      <span>{{ modelValue === undefined ? t("form.unset") : String(modelValue) }}</span>
    </label>

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
      <input
        :id="inputId"
        class="field code"
        :value="secretName"
        :placeholder="t('form.secret')"
        @input="
          (e) => {
            const v = (e.target as HTMLInputElement).value.trim();
            emit('update:modelValue', v ? { secret: v } : undefined);
          }
        "
      />
      <small>{{ t("form.secretHint") }}</small>
    </template>

    <template v-else-if="kind === 'string' && optionKind">
      <input
        :id="inputId"
        class="field"
        :class="{ invalid: invalidPattern, warn: unknownRef }"
        :list="`${inputId}-list`"
        :value="stringValue"
        :placeholder="t('form.choose')"
        autocomplete="off"
        @input="setString(($event.target as HTMLInputElement).value)"
      />
      <datalist :id="`${inputId}-list`">
        <option v-for="c in choices" :key="c.id" :value="c.id">{{ c.name }}</option>
      </datalist>
      <small v-if="chosen" class="chosen">{{ chosen.name }}</small>
      <small v-else-if="unknownRef" class="warn">{{ t("form.notFound") }}</small>
      <small v-else-if="!hostOptions.available">{{ t("form.noHost") }}</small>
      <button type="button" class="refresh" @click="refreshOptions()">{{ t("form.refresh") }}</button>
    </template>

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

    <small v-if="hint && kind !== 'secret' && !optionKind">{{ hint }}</small>
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

label code {
  font-size: 12px;
  color: var(--text);
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

.refresh:hover {
  text-decoration: underline;
}

.check {
  gap: 8px;
  align-items: center;
  color: var(--muted);
}
</style>
