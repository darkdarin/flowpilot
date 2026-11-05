<script setup lang="ts">
import { Handle, Position, type NodeProps } from "@vue-flow/core";
import { computed } from "vue";
import { languageName, t } from "../i18n";
import { play, playing } from "../playback";
import {
  currentTurn,
  debug,
  edges,
  hostOptions,
  idleTaps,
  nodeIssues,
  nodeWarnings,
  specs,
  type FpNodeData,
} from "../store";
import { fieldOptionTitles, fieldTitle, nodeDescription, nodeTitle, portDescription } from "../texts";
import { categoryColor, typeColor, typeShort } from "../types";
import ClientInput from "./ClientInput.vue";

const props = defineProps<NodeProps<FpNodeData>>();

const spec = computed(() => specs.value.get(props.data.type));
const problems = computed(() => nodeIssues.value.get(props.id) ?? []);
const cautions = computed(() => nodeWarnings.value.get(props.id) ?? []);
const run = computed(() => (debug.enabled ? currentTurn.value?.nodes[props.id] : undefined));

// A debug tap idles (greyed out, nothing reaches it) until live debug is on; then it shows what it
// recorded or saw in the selected turn.
const idle = computed(() => idleTaps.value.has(props.id));
const tapView = computed(() => {
  const turn = currentTurn.value;
  if (!debug.enabled || !spec.value?.debug_tap || !turn) return null;
  const texts = turn.texts[props.id] ?? {};
  return {
    recordings: turn.recordings.filter((r) => r.node === props.id),
    texts: spec.value.inputs.filter((p) => texts[p.name]).map((p) => ({ port: p.name, ...texts[p.name] })),
  };
});

const connected = computed(() => {
  const set = new Set<string>();
  for (const e of edges.value) {
    if (e.source === props.id) set.add(`out:${e.sourceHandle}`);
    if (e.target === props.id) set.add(`in:${e.targetHandle}`);
  }
  return set;
});

function portTip(side: "inputs" | "outputs", name: string, type: string, kind: string): string {
  const desc = portDescription(spec.value, side, name);
  return desc ? `${desc}\n${type} · ${kind}` : `${type} · ${kind}`;
}

function portRun(port: string) {
  return debug.enabled ? currentTurn.value?.ports[`${props.id}.${port}`] : undefined;
}

// The setting that says most about the node, shown under its title by name rather than raw value:
// a choice from a list first (a Wyoming service, an entity), then a required one, then any.
const summary = computed(() => {
  const cfg = props.data.config;
  const props_ = spec.value?.config_schema.properties ?? {};
  const required = new Set(spec.value?.config_schema.required ?? []);
  const shown = Object.entries(cfg).filter(([, v]) => typeof v === "string" || typeof v === "number");
  const rank = ([key]: [string, unknown]) =>
    props_[key]?.["x-flowpilot-options"] ? 0 : required.has(key) ? 1 : 2;
  const [key, value] = [...shown].sort((a, b) => rank(a) - rank(b))[0] ?? [];
  if (key === undefined) return "";
  const schema = props_[key] ?? {};
  const kind = schema["x-flowpilot-options"];
  const known = kind ? hostOptions.value.options[kind]?.find((o) => o.id === value) : undefined;
  const label = fieldOptionTitles(spec.value, key)[String(value)];
  const text =
    known?.name || label || (schema.format === "language" ? languageName(String(value)) : String(value));
  return `${fieldTitle(spec.value, key) || key}: ${text}`;
});
</script>

<template>
  <div
    class="fp-node"
    :class="{ unknown: !spec, problem: problems.length, idle, [`run-${run?.status}`]: run }"
    :style="{ '--cat': categoryColor(spec?.category ?? 'logic') }"
  >
    <header>
      <div class="title">
        <span class="name" :title="nodeDescription(spec)">{{ nodeTitle(spec, data.type) }}</span>
        <span v-if="problems.length" class="badge problem" :title="problems.map((p) => p.message).join('\n')">
          {{ problems.length }}
        </span>
        <span v-if="cautions.length" class="badge caution" :title="cautions.map((p) => p.message).join('\n')">
          !
        </span>
        <span v-if="idle" class="badge hint" :title="t('node.tapIdle')" :aria-label="t('node.tapIdle')">?</span>
        <span v-if="run" class="badge run" :title="run.error ?? ''">
          <template v-if="run.status === 'running'">{{ t("status.running") }}</template>
          <template v-else>{{ (run.durationMs ?? 0) < 10 ? (run.durationMs ?? 0).toFixed(1) : Math.round(run.durationMs ?? 0) }} ms</template>
        </span>
      </div>
      <div class="sub">
        <code>{{ id }}</code>
        <span v-if="summary" class="summary">{{ summary }}</span>
      </div>
    </header>

    <div v-if="spec" class="ports">
      <div class="col in">
        <div v-for="p in spec.inputs" :key="p.name" class="port" :style="{ '--pin': typeColor(p.type) }">
          <Handle
            :id="p.name"
            type="target"
            :position="Position.Left"
            :class="[p.kind, { connected: connected.has(`in:${p.name}`) }]"
            :title="portTip('inputs', p.name, p.type, p.kind)"
          />
          <span class="label" :class="{ optional: !p.required }" :title="portTip('inputs', p.name, p.type, p.kind)">{{ p.name }}</span>
          <span class="type">{{ typeShort(p.type) }}</span>
        </div>
      </div>
      <div class="col out">
        <div v-for="p in spec.outputs" :key="p.name" class="port" :style="{ '--pin': typeColor(p.type) }">
          <span v-if="portRun(p.name)" class="count" :class="portRun(p.name)!.state">
            {{ portRun(p.name)!.state === "absent" ? "–" : portRun(p.name)!.items }}
          </span>
          <span class="type">{{ typeShort(p.type) }}</span>
          <span class="label" :title="portTip('outputs', p.name, p.type, p.kind)">{{ p.name }}</span>
          <Handle
            :id="p.name"
            type="source"
            :position="Position.Right"
            :class="[p.kind, { connected: connected.has(`out:${p.name}`) }]"
            :title="portTip('outputs', p.name, p.type, p.kind)"
          />
        </div>
      </div>
    </div>
    <p v-else class="missing">{{ t("inspector.unknownType") }}</p>
    <ClientInput v-if="debug.enabled && data.type === 'client.source'" />
    <div v-if="tapView" class="tap nodrag nowheel">
      <div v-for="rec in tapView.recordings" :key="rec.file" class="tap-rec">
        <button
          class="play"
          :class="{ on: playing === rec.file }"
          :title="playing === rec.file ? t('debug.stopPlaying') : t('debug.play')"
          :aria-label="playing === rec.file ? t('debug.stopPlaying') : t('debug.play')"
          @click.stop="play(rec.file)"
        >
          <svg v-if="playing === rec.file" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="4" width="8" height="8" rx="1" /></svg>
          <svg v-else viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7.5-4.5z" /></svg>
        </button>
        <span class="secs">{{ rec.seconds.toFixed(1) }} s</span>
      </div>
      <div v-for="item in tapView.texts" :key="item.port" class="tap-text">
        <div class="tap-head">
          <code>{{ item.port }}</code>
          <span v-if="item.items > 1">{{ t("node.tapPieces", { n: item.items }) }}</span>
          <span v-if="item.partials">{{ t("node.tapPartials", { n: item.partials }) }}</span>
        </div>
        <p v-if="item.error" class="tap-error">{{ item.error }}</p>
        <p class="tap-body">{{ item.text || "—" }}</p>
      </div>
      <p v-if="!tapView.recordings.length && !tapView.texts.length" class="tap-empty">{{ t("node.tapEmpty") }}</p>
    </div>
    <p v-if="run?.error" class="error">{{ run.error }}</p>
  </div>
</template>

<style scoped>
.fp-node {
  min-width: 230px;
  max-width: 300px;
  border-radius: var(--radius-node);
  background: var(--node);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
  overflow: visible;
}

header {
  position: relative;
  padding: 9px 12px 8px 14px;
  border-radius: var(--radius-node) var(--radius-node) 0 0;
  background: linear-gradient(90deg, color-mix(in srgb, var(--cat) 22%, var(--node-head)), var(--node-head) 70%);
  border-bottom: 1px solid var(--line);
}

header::before {
  content: "";
  position: absolute;
  left: 0;
  top: 8px;
  bottom: 8px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--cat);
}

.title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.name {
  font-weight: 600;
  font-size: 13px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sub {
  display: flex;
  gap: 8px;
  color: var(--muted);
  font-size: 11.5px;
  min-width: 0;
}

.summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.badge {
  font-size: 11px;
  font-weight: 600;
  padding: 0 6px;
  border-radius: 9px;
  line-height: 18px;
}

.badge.problem {
  background: color-mix(in srgb, var(--danger) 18%, transparent);
  color: var(--danger);
}

.badge.caution {
  background: color-mix(in srgb, var(--warn) 20%, transparent);
  color: var(--warn);
}

.badge.run {
  background: color-mix(in srgb, var(--ok) 16%, transparent);
  color: var(--ok);
  font-variant-numeric: tabular-nums;
}

.run-running .badge.run {
  background: color-mix(in srgb, var(--warn) 16%, transparent);
  color: var(--warn);
}

.run-error .badge.run,
.run-timeout .badge.run,
.run-cancelled .badge.run {
  background: color-mix(in srgb, var(--danger) 16%, transparent);
  color: var(--danger);
}

.run-running {
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--warn) 60%, transparent), var(--shadow);
}

.run-error,
.run-timeout {
  border-color: var(--danger);
}

.problem:not(.badge) {
  border-color: color-mix(in srgb, var(--danger) 70%, var(--line));
}

.ports {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  padding: 8px 0 10px;
}

.col {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.port {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 6px;
  height: 22px;
  line-height: 22px;
  padding: 0 14px;
  white-space: nowrap;
}

.out .port {
  justify-content: flex-end;
}

.label {
  font-weight: 500;
}

.label.optional {
  color: var(--muted);
}

.type {
  font-size: 10.5px;
  color: var(--pin);
  opacity: 0.9;
}

.count {
  font-family: var(--font-code);
  font-size: 10.5px;
  padding: 0 5px;
  border-radius: 4px;
  line-height: 16px;
  background: color-mix(in srgb, var(--pin) 20%, transparent);
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.count.absent {
  background: transparent;
  color: var(--faint);
}

.count.error {
  background: color-mix(in srgb, var(--danger) 25%, transparent);
}

.missing,
.error {
  margin: 0;
  padding: 8px 12px 10px;
  font-size: 11.5px;
  color: var(--danger);
  max-width: 300px;
}

.unknown {
  border-style: dashed;
}

.idle {
  border-style: dashed;
  box-shadow: none;
  filter: grayscale(1);
  opacity: 0.55;
}

.badge.hint {
  background: var(--line);
  color: var(--muted);
  cursor: help;
}

.tap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 12px 10px;
  border-top: 1px solid var(--line);
  max-width: 300px;
  cursor: auto;
}

.tap-rec {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tap-rec .play {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: color-mix(in srgb, var(--cat-debug) 22%, transparent);
  color: var(--text);
  cursor: pointer;
}

.tap-rec .play.on {
  background: var(--cat-debug);
  color: var(--primary-text);
}

.tap-rec svg {
  width: 13px;
  height: 13px;
  fill: currentColor;
}

.secs,
.tap-head,
.tap-empty {
  font-size: 11.5px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.tap-head {
  display: flex;
  gap: 8px;
  margin-bottom: 3px;
}

.tap-body {
  margin: 0;
  max-height: 140px;
  overflow-y: auto;
  padding: 5px 7px;
  border-radius: 5px;
  background: var(--panel-raised);
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
}

.tap-error {
  margin: 0 0 3px;
  font-size: 11.5px;
  color: var(--danger);
}

.tap-empty {
  margin: 0;
}
</style>
