<script setup lang="ts">
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from "@vue-flow/core";
import { computed } from "vue";
import { brokenEdges, currentTurn, debug } from "../store";
import { typeColor } from "../types";

const props = defineProps<EdgeProps<{ type: string }>>();

const path = computed(() =>
  getBezierPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition,
    curvature: 0.45,
  }),
);

const color = computed(() => typeColor(props.data?.type ?? ""));

// In live debug a wire is lit once its source port produced data, and shows the item count.
const flow = computed(() => {
  if (!debug.enabled || !currentTurn.value) return null;
  return currentTurn.value.ports[`${props.source}.${props.sourceHandleId}`] ?? { items: 0, state: "idle" };
});

const broken = computed(() => brokenEdges.value.has(props.id));
</script>

<template>
  <BaseEdge
    :id="id"
    :path="path[0]"
    class="typed-edge"
    :class="[flow ? `flow-${flow.state}` : '', { selected, broken, dimmed: flow && flow.state === 'idle' }]"
    :style="{ '--wire': color }"
    :interaction-width="18"
  />
  <EdgeLabelRenderer v-if="flow && flow.items > 0">
    <div
      class="edge-count"
      :style="{
        transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)`,
        '--wire': color,
      }"
    >
      {{ flow.items }}
    </div>
  </EdgeLabelRenderer>
</template>

<style>
.typed-edge.vue-flow__edge-path {
  stroke: var(--wire);
  stroke-linecap: round;
  transition: opacity 0.2s, stroke-width 0.2s;
}

.vue-flow__edge.selected .typed-edge,
.typed-edge.selected {
  stroke-width: 4;
  filter: drop-shadow(0 0 4px color-mix(in srgb, var(--wire) 60%, transparent));
}

.typed-edge.broken {
  stroke-dasharray: 4 6;
}

.typed-edge.dimmed,
.typed-edge.flow-absent {
  opacity: 0.25;
}

.typed-edge.flow-flowing {
  stroke-dasharray: 10 8;
  animation: wire-flow 0.6s linear infinite;
  stroke-width: 3.5;
}

.typed-edge.flow-end {
  stroke-width: 3.5;
  filter: drop-shadow(0 0 3px color-mix(in srgb, var(--wire) 55%, transparent));
}

.typed-edge.flow-error {
  stroke: var(--danger);
  stroke-dasharray: 3 5;
}

@keyframes wire-flow {
  to {
    stroke-dashoffset: -18;
  }
}

.edge-count {
  position: absolute;
  pointer-events: none;
  font-family: var(--font-code);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--panel-raised);
  border: 1.5px solid var(--wire);
  color: var(--text);
}
</style>
