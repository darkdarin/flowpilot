<script setup lang="ts">
// JSON as Chrome's DevTools show it: keys and values in colour, objects and arrays fold. Folded,
// a branch shows a preview of what is inside.
import { computed, ref } from "vue";

defineOptions({ name: "JsonView" });

const props = withDefaults(
  defineProps<{ value: unknown; name?: string; depth?: number; comma?: boolean }>(),
  { name: undefined, depth: 0, comma: false },
);

const isArray = computed(() => Array.isArray(props.value));
// an empty object or array is a value of its own: `[]`, `{}`
const isBranch = computed(
  () => props.value !== null && typeof props.value === "object" && Object.keys(props.value).length > 0,
);
const entries = computed<[string, unknown][]>(() =>
  isBranch.value ? Object.entries(props.value as Record<string, unknown>) : [],
);
// the first levels open, deeper ones folded
const open = ref(props.depth < 2);

function scalar(value: unknown): { text: string; kind: string } {
  if (value === null) return { text: "null", kind: "null" };
  if (Array.isArray(value)) return { text: "[]", kind: "punct" };
  if (typeof value === "object") return { text: "{}", kind: "punct" };
  if (typeof value === "string") return { text: JSON.stringify(value).slice(1, -1).replaceAll("\\n", "\n"), kind: "str" };
  if (typeof value === "number") return { text: String(value), kind: "num" };
  if (typeof value === "boolean") return { text: String(value), kind: "bool" };
  return { text: String(value), kind: "str" };
}

/** A folded branch's preview: `{text: "…", data: {…}}`, `Array(3)`. */
const preview = computed(() => {
  if (isArray.value) return `Array(${entries.value.length})`;
  const parts = entries.value.slice(0, 4).map(([k, v]) => {
    if (v !== null && typeof v === "object") return `${k}: ${Array.isArray(v) ? `Array(${v.length})` : "{…}"}`;
    const s = scalar(v);
    const short = s.text.length > 24 ? `${s.text.slice(0, 23)}…` : s.text;
    return `${k}: ${s.kind === "str" ? `"${short}"` : short}`;
  });
  return `{${parts.join(", ")}${entries.value.length > 4 ? ", …" : ""}}`;
});
</script>

<template>
  <div class="jv" :class="{ root: depth === 0 }">
    <template v-if="isBranch">
      <span class="line" @click="open = !open">
        <span class="toggle" :class="{ open }" aria-hidden="true">▸</span>
        <span v-if="name !== undefined" class="key">{{ name }}</span><span v-if="name !== undefined" class="punct">: </span>
        <template v-if="!open">
          <span class="preview">{{ preview }}</span><span v-if="comma" class="punct">,</span>
        </template>
        <span v-else class="punct">{{ isArray ? "[" : "{" }}</span>
      </span>
      <template v-if="open">
        <div class="children">
          <JsonView
            v-for="([key, child], i) in entries"
            :key="key"
            :value="child"
            :name="isArray ? undefined : key"
            :depth="depth + 1"
            :comma="i < entries.length - 1"
          />
        </div>
        <span class="punct close">{{ isArray ? "]" : "}" }}</span><span v-if="comma" class="punct">,</span>
      </template>
    </template>
    <span v-else class="leaf">
      <span v-if="name !== undefined" class="key">{{ name }}</span><span v-if="name !== undefined" class="punct">: </span>
      <span :class="scalar(value).kind">{{ scalar(value).kind === "str" ? `"${scalar(value).text}"` : scalar(value).text }}</span><span v-if="comma" class="punct">,</span>
    </span>
  </div>
</template>

<style scoped>
.jv {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 11.5px;
  line-height: 1.5;
}

.children {
  padding-left: 14px;
  border-left: 1px dotted var(--line);
  margin-left: 4px;
}

.line {
  cursor: pointer;
  user-select: none;
}

.line:hover .preview,
.line:hover .key {
  text-decoration: underline dotted;
}

.toggle {
  display: inline-block;
  width: 12px;
  color: var(--faint);
  transition: transform 0.1s;
}

.toggle.open {
  transform: rotate(90deg);
}

.leaf {
  display: block;
  padding-left: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.close {
  padding-left: 12px;
}

/* DevTools colours: the dark theme first, the light one after */
.key {
  color: #d58fe8;
}

.punct,
.preview {
  color: var(--muted);
}

.str {
  color: #f28b74;
}

.num {
  color: #8cb4ff;
}

.bool,
.null {
  color: #5fd3c0;
}

:global(:root[data-theme="light"]) .key {
  color: #a347ba;
}

:global(:root[data-theme="light"]) .str {
  color: #c1442d;
}

:global(:root[data-theme="light"]) .num {
  color: #1a5fb4;
}

:global(:root[data-theme="light"]) .bool,
:global(:root[data-theme="light"]) .null {
  color: #0b7d6f;
}
</style>
