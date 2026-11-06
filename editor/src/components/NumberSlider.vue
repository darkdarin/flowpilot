<script setup lang="ts">
// A number setting with both bounds: a slider for quick changes and a small field for exact values.
// Wide positive ranges (1…10000 ms) use a logarithmic scale, so small values stay reachable.
// An unset value keeps the node's default: the slider sits on it, dimmed, and the field shows it
// as a placeholder; the reset button returns to it.
import { computed } from "vue";
import { t } from "../i18n";

const props = defineProps<{
  inputId: string;
  modelValue: number | undefined;
  min: number;
  max: number;
  integer: boolean;
  fallback?: number; // the default when not set
}>();
const emit = defineEmits<{ "update:modelValue": [value: number | undefined] }>();

const STEPS = 1000;
const log = computed(() => props.min > 0 && props.max / props.min >= 100);

// A round step for linear sliders: 1, 2 or 5 times a power of ten, about 1/100 of the range.
const step = computed(() => {
  if (props.integer && props.max - props.min <= 200) return 1;
  const raw = (props.max - props.min) / 100;
  const power = 10 ** Math.floor(Math.log10(raw));
  const nice = [1, 2, 5, 10].find((m) => m * power >= raw)! * power;
  return props.integer ? Math.max(1, Math.round(nice)) : nice;
});

function round(value: number): number {
  if (log.value) {
    // two significant digits: 1, 1.5, 22, 350, 4700…
    const power = 10 ** (Math.floor(Math.log10(value)) - 1);
    value = Math.round(value / power) * power;
  } else {
    value = Math.round((value - props.min) / step.value) * step.value + props.min;
  }
  value = Math.min(props.max, Math.max(props.min, value));
  return props.integer ? Math.round(value) : Number(value.toPrecision(12));
}

function toPosition(value: number): number {
  const v = Math.min(props.max, Math.max(props.min, value));
  const share = log.value
    ? Math.log(v / props.min) / Math.log(props.max / props.min)
    : (v - props.min) / (props.max - props.min);
  return Math.round(share * STEPS);
}

function fromPosition(position: number): number {
  const share = position / STEPS;
  return log.value
    ? props.min * (props.max / props.min) ** share
    : props.min + share * (props.max - props.min);
}

// Digits after the point the slider can produce, shown always, so values do not jump while sliding
// (a value typed with more digits keeps them).
function decimalsOf(value: number): number {
  const text = String(value);
  return text.includes("e") ? 0 : (text.split(".")[1]?.length ?? 0);
}
const decimals = computed(() => {
  if (props.integer) return 0;
  if (log.value) return Math.max(0, 1 - Math.floor(Math.log10(props.min))); // 2 significant at min
  return decimalsOf(Number(step.value.toPrecision(12)));
});
function format(value: number | undefined): string {
  return value === undefined ? "" : value.toFixed(Math.max(decimals.value, decimalsOf(value)));
}

const unset = computed(() => props.modelValue === undefined);
const shown = computed(() => props.modelValue ?? props.fallback ?? props.min);
const position = computed(() => toPosition(shown.value));

function onSlide(e: Event) {
  emit("update:modelValue", round(fromPosition(Number((e.target as HTMLInputElement).value))));
}

function onType(e: Event) {
  const text = (e.target as HTMLInputElement).value.trim().replace(",", ".");
  if (text === "") return emit("update:modelValue", undefined);
  const n = Number(text);
  if (Number.isNaN(n)) return;
  emit("update:modelValue", props.integer ? Math.trunc(n) : n);
}

const outOfRange = computed(
  () => props.modelValue !== undefined && (props.modelValue < props.min || props.modelValue > props.max),
);
</script>

<template>
  <div class="slider" :class="{ unset }">
    <input
      type="range"
      class="range"
      :min="0"
      :max="STEPS"
      :value="position"
      :style="{ '--fill': `${(position / STEPS) * 100}%` }"
      :aria-label="t('form.slider')"
      @input="onSlide"
    />
    <input
      :id="inputId"
      class="field exact"
      :class="{ invalid: outOfRange }"
      inputmode="decimal"
      :value="format(modelValue)"
      :placeholder="format(fallback)"
      @change="onType"
      @keydown.enter="onType"
    />
    <button
      type="button"
      class="reset"
      :class="{ hidden: unset }"
      :disabled="unset"
      :title="t('form.reset')"
      :aria-label="t('form.reset')"
      @click="emit('update:modelValue', undefined)"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
    </button>
  </div>
</template>

<style scoped>
.slider {
  display: flex;
  align-items: center;
  gap: 8px;
}

.range {
  flex: 1;
  min-width: 0;
  height: 18px;
  margin: 0;
  background: transparent;
  appearance: none;
  cursor: pointer;
}

.range::-webkit-slider-runnable-track {
  height: 4px;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--focus) var(--fill), var(--line) var(--fill));
}

.range::-moz-range-track {
  height: 4px;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--focus) var(--fill), var(--line) var(--fill));
}

.range::-webkit-slider-thumb {
  appearance: none;
  width: 14px;
  height: 14px;
  margin-top: -5px;
  border-radius: 50%;
  border: 2px solid var(--focus);
  background: var(--panel);
}

.range::-moz-range-thumb {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid var(--focus);
  background: var(--panel);
}

.range:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
  border-radius: 4px;
}

.unset .range {
  opacity: 0.45;
}

.exact {
  width: 72px;
  flex: none;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.reset {
  display: grid;
  place-items: center;
  flex: none;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.reset:hover:not(:disabled) {
  background: var(--line);
  color: var(--text);
}

.reset.hidden {
  visibility: hidden;
}

.reset svg {
  width: 10px;
  height: 10px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
}
</style>
