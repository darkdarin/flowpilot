<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { JsonSchema } from "../api";
import { t } from "../i18n";
import { issues, nodeIssues, nodeWarnings, removeNode, renameNode, selectedId, selectedNode, specs } from "../store";
import { categoryColor, typeColor, typeShort } from "../types";
import {
  fieldDescription,
  fieldOptionTitles,
  fieldTitle,
  groupTitle,
  nodeDescription,
  nodeTitle,
  portDescription,
} from "../texts";
import SchemaField from "./SchemaField.vue";
import SettingsGroupDialog from "./SettingsGroupDialog.vue";

const spec = computed(() => (selectedNode.value ? specs.value.get(selectedNode.value.data!.type) : undefined));
const allProps = computed(() => Object.entries(spec.value?.config_schema.properties ?? {}));
type Field = [string, JsonSchema];
// grouped settings live in their group's dialog; the panel shows the group as one entry, where
// its first setting would be
const items = computed(() => {
  const list: ({ key: string; schema: JsonSchema } | { group: string; fields: Field[] })[] = [];
  const groups = new Map<string, Field[]>();
  for (const [key, schema] of allProps.value) {
    const group = schema["x-flowpilot-group"];
    if (!group) list.push({ key, schema });
    else if (groups.has(group)) groups.get(group)!.push([key, schema]);
    else {
      const fields: Field[] = [[key, schema]];
      groups.set(group, fields);
      list.push({ group, fields });
    }
  }
  return list;
});
const openFields = computed(() => {
  for (const item of items.value) if ("group" in item && item.group === openGroup.value) return item.fields;
  return [];
});
const openGroup = ref<string | null>(null);
watch(selectedId, () => (openGroup.value = null));

/** What a group is set to, in short: the labels of its choices (set or default). */
function groupSummary(fields: Field[]): string {
  const config = selectedNode.value?.data?.config ?? {};
  return fields
    .filter(([, s]) => s.enum)
    .map(([key, s]) => {
      const value = config[key] ?? s.default;
      return value === undefined ? "" : (fieldOptionTitles(spec.value, key)[String(value)] ?? String(value));
    })
    .filter(Boolean)
    .join(" · ");
}
const required = computed(() => new Set(spec.value?.config_schema.required ?? []));
const problems = computed(() => (selectedId.value ? (nodeIssues.value.get(selectedId.value) ?? []) : []));
// warnings tied to a setting are shown next to it, in the UI language
const cautions = computed(() =>
  (selectedId.value ? (nodeWarnings.value.get(selectedId.value) ?? []) : []).filter((w) => !w.field),
);
const graphProblems = computed(() => issues.value.filter((i) => !i.node));

const idDraft = ref("");
const idTaken = ref(false);
watch(selectedId, (id) => {
  idDraft.value = id ?? "";
  idTaken.value = false;
}, { immediate: true });

function commitId() {
  if (!selectedId.value) return;
  idTaken.value = !renameNode(selectedId.value, idDraft.value.trim());
  if (idTaken.value) idDraft.value = selectedId.value;
}

function setConfig(key: string, value: unknown) {
  const node = selectedNode.value;
  if (!node?.data) return;
  const config = { ...node.data.config };
  if (value === undefined) delete config[key];
  else config[key] = value;
  node.data = { ...node.data, config };
}
</script>

<template>
  <aside class="inspector">
    <template v-if="selectedNode">
      <header :style="{ '--cat': categoryColor(spec?.category ?? 'logic') }">
        <h2>{{ nodeTitle(spec, selectedNode.data!.type) }}</h2>
        <code>{{ selectedNode.data!.type }}<template v-if="spec">&ensp;v{{ spec.version }}</template></code>
        <p v-if="nodeDescription(spec)">{{ nodeDescription(spec) }}</p>
        <p v-if="!spec" class="bad">{{ t("inspector.unknownType") }}</p>
      </header>

      <ul v-if="problems.length" class="problems">
        <li v-for="p in problems" :key="p.path + p.code">{{ p.message }}</li>
      </ul>

      <ul v-if="cautions.length" class="problems cautions">
        <li v-for="w in cautions" :key="w.path">{{ w.message }}</li>
      </ul>

      <section>
        <label for="node-id" class="lbl">{{ t("inspector.id") }}</label>
        <input id="node-id" v-model="idDraft" class="field code" :class="{ invalid: idTaken }" @change="commitId" @keydown.enter="commitId" />
        <small v-if="idTaken" class="bad">{{ t("inspector.idTaken") }}</small>
      </section>

      <section v-if="spec">
        <h3>{{ t("inspector.settings") }}</h3>
        <p v-if="!allProps.length" class="muted">{{ t("inspector.noSettings") }}</p>
        <template v-for="item in items" :key="selectedNode.id + ('group' in item ? '#' + item.group : item.key)">
          <SchemaField
            v-if="'key' in item"
            :name="item.key"
            :schema="item.schema"
            :required="required.has(item.key)"
            :label="fieldTitle(spec, item.key)"
            :description="fieldDescription(spec, item.key)"
            :node-type="selectedNode.data!.type"
            :config="selectedNode.data!.config"
            :option-titles="fieldOptionTitles(spec, item.key)"
            :model-value="selectedNode.data!.config[item.key]"
            @update:model-value="setConfig(item.key, $event)"
          />
          <button v-else type="button" class="group" @click="openGroup = item.group">
            <span class="gtext">
              <span class="gtitle">{{ groupTitle(spec, item.group) }}</span>
              <span v-if="groupSummary(item.fields)" class="gsummary">{{ groupSummary(item.fields) }}</span>
            </span>
            <span class="chev" aria-hidden="true">›</span>
          </button>
        </template>
      </section>
      <SettingsGroupDialog
        v-if="spec && openGroup"
        :spec="spec"
        :group="openGroup"
        :fields="openFields"
        :required="required"
        :node-id="selectedNode.id"
        :node-type="selectedNode.data!.type"
        :config="selectedNode.data!.config"
        @set="setConfig"
        @close="openGroup = null"
      />

      <section v-if="spec">
        <h3>{{ t("inspector.ports") }}</h3>
        <div class="ports">
          <template v-for="side in (['inputs', 'outputs'] as const)" :key="side">
            <h4 v-if="spec[side].length">{{ t(side === "inputs" ? "inspector.inputs" : "inspector.outputs") }}</h4>
            <div v-for="p in spec[side]" :key="side + p.name" class="port" :style="{ '--pin': typeColor(p.type) }">
              <div class="phead">
                <i :class="p.kind" />
                <span class="pname">{{ p.name }}</span>
                <code :title="p.type">{{ p.type.includes("|") ? `${typeShort(p.type)}${p.var ? ` (${p.var})` : ""}` : p.type }}</code>
                <span v-if="side === 'inputs' && !p.required" class="muted opt">{{ t("inspector.optional") }}</span>
              </div>
              <p v-if="portDescription(spec, side, p.name)" class="pdesc">{{ portDescription(spec, side, p.name) }}</p>
            </div>
          </template>
        </div>
        <p v-if="spec.host_ops.length" class="muted">
          {{ t("inspector.needsHost") }}: <code>{{ spec.host_ops.join(", ") }}</code>
        </p>
      </section>

      <button v-if="!spec?.fixed" class="btn danger" @click="removeNode(selectedNode.id)">{{ t("inspector.delete") }}</button>
      <p v-else class="fixed-note">{{ t("inspector.fixed") }}</p>
    </template>

    <template v-else>
      <p class="muted empty">{{ t("inspector.empty") }}</p>
      <section v-if="graphProblems.length">
        <h3>{{ t("issues.title") }}</h3>
        <ul class="problems">
          <li v-for="p in graphProblems" :key="p.path + p.code">
            {{ p.message }}<br /><code>{{ p.path }}</code>
          </li>
        </ul>
      </section>
    </template>
  </aside>
</template>

<style scoped>
.inspector {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 16px;
  background: var(--panel);
  border-left: 1px solid var(--line);
  overflow-y: auto;
  min-height: 0;
}

header {
  border-left: 3px solid var(--cat);
  padding-left: 10px;
}

h2 {
  margin: 0 0 2px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.01em;
}

header code {
  color: var(--muted);
}

header p {
  margin: 8px 0 0;
  color: var(--muted);
}

h3 {
  margin: 0 0 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
}

section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

section > h3 {
  margin-bottom: -2px;
}

.lbl {
  font-size: 12px;
  color: var(--muted);
  margin-bottom: -8px;
}

.muted {
  color: var(--muted);
  margin: 0;
}

.empty {
  margin-top: 8px;
}

.bad {
  color: var(--danger);
}

.problems {
  margin: 0;
  padding: 10px 12px 10px 28px;
  border-radius: var(--radius-ctl);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 35%, transparent);
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.problems.cautions {
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  border-color: color-mix(in srgb, var(--warn) 40%, transparent);
}

.problems code {
  color: var(--muted);
}

.ports {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 12px;
}

.ports h4 {
  margin: 4px 0 0;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--faint);
}

.phead {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.pdesc {
  margin: 2px 0 0 15px;
  color: var(--muted);
  line-height: 1.4;
}

.phead i {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--pin);
}

.phead i.value {
  border-radius: 1px;
  transform: rotate(45deg);
  width: 7px;
  height: 7px;
}

.pname {
  font-weight: 500;
}

.phead code {
  color: var(--pin);
  overflow: hidden;
  text-overflow: ellipsis;
}

.opt {
  margin-left: auto;
  font-size: 11px;
}

.group {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-ctl);
  background: var(--panel-raised);
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.group:hover {
  border-color: color-mix(in srgb, var(--text) 25%, var(--line));
}

.gtext {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.gtitle {
  font-weight: 500;
}

.gsummary {
  color: var(--muted);
  font-size: 12px;
}

.chev {
  color: var(--muted);
  font-size: 18px;
  line-height: 1;
}

.btn.danger {
  align-self: flex-start;
}

.fixed-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
</style>
