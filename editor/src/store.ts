// Editor state: catalog, the open graph (as Vue Flow nodes/edges), draft autosave, deploy, live debug.

import type { Edge, Node } from "@vue-flow/core";
import { type Ref, computed, reactive, ref, shallowRef, watch } from "vue";
import {
  Api,
  ApiError,
  type Catalog,
  type GraphDoc,
  type GraphSummary,
  type HostOption,
  type HostOptions,
  type Issue,
  type NodeInfo,
  type PortInfo,
  type SecretInfo,
  type TraceMsg,
  TraceSocket,
} from "./api";
import { t } from "./i18n";

export interface FpNodeData {
  type: string;
  config: Record<string, unknown>;
  setting_inputs?: string[]; // settings made inputs of the node
}

export type FpNode = Node<FpNodeData>;
export type FpEdge = Edge<{ type: string }>;

export interface NodeRun {
  status: string; // running | ok | error | timeout | cancelled
  startMs: number;
  durationMs?: number;
  error?: string;
}

export interface PortRun {
  items: number;
  state: string; // flowing | end | error | absent
  firstMs?: number;
  /** when the output ended (ms from the turn's start) */
  endMs?: number;
}

export interface TurnRun {
  id: string;
  status: string; // running | ok | degraded | timeout
  durationMs?: number;
  nodes: Record<string, NodeRun>;
  ports: Record<string, PortRun>;
  hostCalls: { node: string; op: string; t: number }[];
  /** what every node passed on (trace event "port.data", live debug only), in order */
  outputs: OutputData[];
  /** nodes' journals (trace event "log", live debug only), by node, in order */
  logs: Record<string, LogEntry[]>;
  /** what nodes reported about the turn in their own trace events (levels, counts), by node */
  notes: Record<string, { event: string; t: number; attrs: TraceMsg["attrs"] }[]>;
}

/** What one output of a node passed on in a watched turn. */
export interface OutputData {
  node: string;
  port: string;
  type: string;
  /** when its first item went out (ms from the turn's start) */
  t: number;
  items: number;
  state: string;
  /** text types: the text (recognized speech: its final text) */
  text?: string;
  partials?: number;
  /** other data, as JSON */
  json?: string;
  /** items of a stream left out of the JSON */
  more?: number;
  /** audio: the recording and its length */
  file?: string;
  seconds?: number;
  /** it passed on what this output ("node.port") sent */
  same?: string;
  error?: string;
}

export interface LogEntry {
  t: number;
  kind: string;
  summary: string;
  detail?: string;
  attrs: TraceMsg["attrs"];
}

const SAVE_DELAY_MS = 600;
const MAX_TURNS = 12;

export const token = ref(localStorage.getItem("flowpilot.token") ?? "");
export const api = shallowRef<Api | null>(null);
export const catalog = shallowRef<Catalog | null>(null);
export const graphs = ref<GraphSummary[]>([]);
export const graphId = ref<string | null>(null);
export const graphName = ref("");
export const extra = ref<Pick<GraphDoc, "settings" | "schema_version">>({ schema_version: 2 });
export const nodes = ref([]) as Ref<FpNode[]>;
export const edges = ref([]) as Ref<FpEdge[]>;
export const issues = ref<Issue[]>([]);
export const warnings = ref<Issue[]>([]);
export const hostOptions = shallowRef<HostOptions>({ available: false, hosts: [], options: {}, strict: [] });
export const hasDraft = ref(false);
export const live = ref(false);
export const saving = ref(false);
export const deploying = ref(false);
export const selectedId = ref<string | null>(null);
export const turnStarting = ref(false);
export const secrets = ref<SecretInfo[]>([]);
export const secretsOpen = ref(false);
export const memoryOpen = ref(false);
export const voicesOpen = ref(false);
export const commandsOpen = ref(false);
export const skillsOpen = ref(false);
export const notice = ref<{ text: string; kind: "info" | "error" } | null>(null);

export const debug = reactive({
  enabled: false,
  connected: false,
  redeployed: false,
  turns: [] as TurnRun[],
  selectedTurn: null as string | null,
});

let loading = false;
let savedSnapshot = ""; // the graph as last loaded or saved; autosave only when it changes
let saveTimer: number | undefined;
let trace: TraceSocket | null = null;

export const specs = computed(() => {
  const map = new Map<string, NodeInfo>();
  for (const n of catalog.value?.nodes ?? []) map.set(n.type, n);
  return map;
});

export const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) ?? null);

export const nodeIssues = computed(() => {
  const map = new Map<string, Issue[]>();
  for (const i of issues.value) if (i.node) map.set(i.node, [...(map.get(i.node) ?? []), i]);
  return map;
});

export const nodeWarnings = computed(() => {
  const map = new Map<string, Issue[]>();
  for (const w of warnings.value) if (w.node) map.set(w.node, [...(map.get(w.node) ?? []), w]);
  return map;
});

// Choices nodes list for their own settings (a Wyoming service's models), keyed by request.
const nodeOptionsCache = new Map<string, Promise<{ options: HostOption[]; error?: string }>>();

export function nodeOptions(type: string, field: string, config: Record<string, unknown>) {
  const key = JSON.stringify([type, field, config]);
  let request = nodeOptionsCache.get(key);
  if (!request) {
    request = api.value!.nodeOptions(type, field, config).catch((err: Error) => ({ options: [], error: err.message }));
    nodeOptionsCache.set(key, request);
  }
  return request;
}

export async function refreshOptions(refresh = true) {
  if (refresh) nodeOptionsCache.clear();
  try {
    hostOptions.value = await api.value!.options(refresh);
  } catch {
    hostOptions.value = { available: false, hosts: [], options: {}, strict: [] };
  }
}

export const brokenEdges = computed(() => {
  const set = new Set<string>();
  for (const i of issues.value) {
    const edge = i.edge === undefined ? undefined : edges.value[i.edge];
    if (edge) set.add(edge.id);
  }
  return set;
});

/** Debug taps (they record or show what reaches them) idle while live debug is off. */
export const idleTaps = computed(() => {
  const set = new Set<string>();
  if (debug.enabled) return set;
  for (const n of nodes.value) if (specs.value.get(n.data?.type ?? "")?.debug_tap) set.add(n.id);
  return set;
});

export const currentTurn = computed<TurnRun | null>(
  () => debug.turns.find((t) => t.id === debug.selectedTurn) ?? debug.turns[0] ?? null,
);

export function flash(text: string, kind: "info" | "error" = "info") {
  notice.value = { text, kind };
  window.setTimeout(() => {
    if (notice.value?.text === text) notice.value = null;
  }, kind === "error" ? 8000 : 3000);
}

// --- graph <-> canvas --------------------------------------------------------------------------

function autoLayout(doc: GraphDoc): Record<string, { x: number; y: number }> {
  const depth = new Map<string, number>(doc.nodes.map((n) => [n.id, 0]));
  for (let pass = 0; pass < doc.nodes.length; pass++) {
    for (const e of doc.edges) {
      const [from] = e.from.split(".");
      const [to] = e.to.split(".");
      depth.set(to, Math.max(depth.get(to) ?? 0, (depth.get(from) ?? 0) + 1));
    }
  }
  const rows = new Map<number, number>();
  const out: Record<string, { x: number; y: number }> = {};
  for (const n of doc.nodes) {
    const d = depth.get(n.id) ?? 0;
    const row = rows.get(d) ?? 0;
    rows.set(d, row + 1);
    out[n.id] = { x: 55 + d * 330, y: 55 + row * 198 }; // on the 11px snap grid
  }
  return out;
}

function portType(nodeType: string, port: string, side: "inputs" | "outputs"): string {
  return specs.value.get(nodeType)?.[side].find((p) => p.name === port)?.type ?? "";
}

function toCanvas(doc: GraphDoc) {
  const positions = { ...autoLayout(doc), ...(doc.ui?.nodes ?? {}) };
  nodes.value = doc.nodes.map((n) => ({
    id: n.id,
    type: "fp",
    position: positions[n.id] ?? { x: 0, y: 0 },
    data: {
      type: n.type,
      config: structuredClone(n.config ?? {}),
      ...(n.setting_inputs?.length ? { setting_inputs: [...n.setting_inputs] } : {}),
    },
    deletable: !specs.value.get(n.type)?.fixed, // the client input and output stay
  }));
  const types = new Map(doc.nodes.map((n) => [n.id, n.type]));
  edges.value = doc.edges.map((e) => {
    const [source, sourceHandle] = splitRef(e.from);
    const [target, targetHandle] = splitRef(e.to);
    return makeEdge(source, sourceHandle, target, targetHandle, portType(types.get(source) ?? "", sourceHandle, "outputs"));
  });
}

function splitRef(ref: string): [string, string] {
  const dot = ref.lastIndexOf(".");
  return [ref.slice(0, dot), ref.slice(dot + 1)];
}

export function makeEdge(source: string, sourceHandle: string, target: string, targetHandle: string, type: string): FpEdge {
  return {
    id: `${source}.${sourceHandle}->${target}.${targetHandle}`,
    type: "typed",
    source,
    sourceHandle,
    target,
    targetHandle,
    data: { type },
  };
}

export function toDoc(): GraphDoc {
  return {
    schema_version: extra.value.schema_version ?? 2,
    id: graphId.value ?? "",
    name: graphName.value,
    ...(extra.value.settings ? { settings: extra.value.settings } : {}),
    nodes: nodes.value.map((n) => {
      const config = n.data?.config ?? {};
      return {
        id: n.id,
        type: n.data!.type,
        ...(Object.keys(config).length ? { config } : {}),
        ...(n.data?.setting_inputs?.length ? { setting_inputs: [...n.data.setting_inputs] } : {}),
      };
    }),
    edges: edges.value.map((e) => ({
      from: `${e.source}.${e.sourceHandle}`,
      to: `${e.target}.${e.targetHandle}`,
    })),
    ui: {
      nodes: Object.fromEntries(
        nodes.value.map((n) => [n.id, { x: Math.round(n.position.x), y: Math.round(n.position.y) }]),
      ),
    },
  };
}

// --- loading and saving ------------------------------------------------------------------------

export async function connect(value: string): Promise<boolean> {
  const client = new Api(value);
  try {
    catalog.value = await client.catalog();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return false;
    throw err;
  }
  token.value = value;
  if (value) localStorage.setItem("flowpilot.token", value);
  api.value = client;
  trace?.close();
  trace = new TraceSocket(value, onTrace, (c) => (debug.connected = c));
  trace.setDebug(debug.enabled);
  await refreshGraphs();
  void refreshSecrets();
  const wanted = localStorage.getItem("flowpilot.graph");
  const pick = graphs.value.find((g) => g.id === wanted) ?? graphs.value.find((g) => g.default) ?? graphs.value[0];
  if (pick) await openGraph(pick.id);
  return true;
}

export async function refreshSecrets() {
  try {
    secrets.value = (await api.value!.secrets()).secrets;
  } catch {
    secrets.value = [];
  }
}

/** Set (value) or remove (null) a secret; graphs using it redeploy. Returns graphs that failed. */
export async function changeSecret(name: string, value: string | null): Promise<Record<string, string>> {
  const result = value === null ? await api.value!.deleteSecret(name) : await api.value!.setSecret(name, value);
  await refreshSecrets();
  if (graphId.value) {
    const detail = await api.value!.graph(graphId.value);
    warnings.value = detail.warnings ?? [];
    live.value = detail.live;
  }
  await refreshGraphs();
  return Object.fromEntries(Object.entries(result.redeployed).filter((e): e is [string, string] => e[1] !== null));
}

export async function refreshGraphs() {
  graphs.value = (await api.value!.graphs()).graphs;
}

/** Turns that name no graph (all of Assist's through the default FlowPilot entities) go here. */
export async function makeDefault(id: string) {
  await api.value!.setDefault(id);
  await refreshGraphs();
}

export async function openGraph(id: string) {
  await flushSave();
  const detail = await api.value!.graph(id);
  const doc = detail.draft ?? detail.deployed!;
  loading = true;
  graphId.value = id;
  graphName.value = doc.name ?? id;
  extra.value = { schema_version: doc.schema_version, settings: doc.settings };
  toCanvas(doc);
  issues.value = detail.issues;
  warnings.value = detail.warnings ?? [];
  void refreshOptions();
  hasDraft.value = detail.draft !== null;
  live.value = detail.live;
  selectedId.value = null;
  debug.turns = [];
  debug.selectedTurn = null;
  debug.redeployed = false;
  localStorage.setItem("flowpilot.graph", id);
  trace?.subscribe(id);
  savedSnapshot = snapshot();
  loading = false;
  if (addMissingFixed().length) flash(t("graph.fixedAdded")); // saved as a draft by the watcher
}

export async function createGraph(id: string, name: string) {
  await api.value!.create(id, name);
  await refreshGraphs();
  await openGraph(id);
}

function snapshot(): string {
  return JSON.stringify(toDoc());
}

function scheduleSave() {
  if (loading || !graphId.value || snapshot() === savedSnapshot) return;
  window.clearTimeout(saveTimer);
  saving.value = true;
  saveTimer = window.setTimeout(() => void save(), SAVE_DELAY_MS);
}

async function save() {
  saveTimer = undefined;
  const id = graphId.value;
  if (!id) return;
  const doc = toDoc();
  savedSnapshot = JSON.stringify(doc);
  try {
    const result = await api.value!.saveDraft(id, doc);
    if (graphId.value === id) {
      issues.value = result.issues;
      warnings.value = result.warnings ?? [];
      hasDraft.value = true;
    }
  } catch (err) {
    flash(String((err as Error).message), "error");
  } finally {
    saving.value = saveTimer !== undefined;
  }
}

async function flushSave() {
  if (saveTimer !== undefined) {
    window.clearTimeout(saveTimer);
    await save();
  }
}

watch([nodes, edges, graphName], scheduleSave, { deep: true });

export async function deploy(): Promise<boolean> {
  const id = graphId.value;
  if (!id) return false;
  deploying.value = true;
  try {
    await flushSave();
    await api.value!.deploy(id);
    hasDraft.value = false;
    live.value = true;
    await refreshGraphs();
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.issues.length) issues.value = err.issues;
    flash(err instanceof ApiError && err.status === 422 ? "invalid" : (err as Error).message, "error");
    return false;
  } finally {
    deploying.value = false;
  }
}

export async function discardDraft() {
  const id = graphId.value;
  if (!id) return;
  window.clearTimeout(saveTimer);
  saveTimer = undefined;
  saving.value = false;
  await api.value!.discardDraft(id);
  await refreshGraphs();
  if (graphs.value.some((g) => g.id === id)) await openGraph(id);
  else if (graphs.value[0]) await openGraph(graphs.value[0].id);
  else graphId.value = null;
}

// --- editing helpers ---------------------------------------------------------------------------

export function uniqueNodeId(type: string): string {
  const base = (type.split(".").pop() ?? "node").replace(/[^a-z0-9_]/g, "_");
  const taken = new Set(nodes.value.map((n) => n.id));
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}${i}`)) return `${base}${i}`;
}

export function addNode(type: string, position: { x: number; y: number }) {
  const id = uniqueNodeId(type);
  nodes.value = [...nodes.value, { id, type: "fp", position, data: { type, config: {} } }];
  selectedId.value = id;
}

/**
 * Every graph has its fixed nodes (client input and output) exactly once. A graph without one
 * (written by hand, imported) gets it added to the draft: the palette does not offer it.
 */
function addMissingFixed(): string[] {
  const present = new Set(nodes.value.map((n) => n.data?.type));
  const missing = (catalog.value?.nodes ?? []).filter((s) => s.fixed && !present.has(s.type));
  if (!missing.length) return [];
  const xs = nodes.value.map((n) => n.position.x);
  const ys = nodes.value.map((n) => n.position.y);
  const y = ys.length ? Math.round(ys.reduce((a, b) => a + b, 0) / ys.length) : 200;
  for (const spec of missing) {
    const left = spec.category === "input";
    const x = left ? Math.min(80, ...xs) - (xs.length ? 330 : 0) : Math.max(720, ...xs) + (xs.length ? 330 : 0);
    nodes.value = [
      ...nodes.value,
      { id: uniqueNodeId(spec.type), type: "fp", position: { x, y }, data: { type: spec.type, config: {} }, deletable: false },
    ];
  }
  return missing.map((s) => s.type);
}

// Copy and paste of nodes (⌘C/⌘V, ⌘D duplicates). The clipboard is in localStorage, not the
// system one (an iframe in Home Assistant may not write it), so it works across graphs and tabs.
// The client input and output are never copied: a graph has each once.
const CLIPBOARD_KEY = "flowpilot.clipboard";
const PASTE_STEP = 40;

interface Clip {
  nodes: { id: string; type: string; config: Record<string, unknown>; position: { x: number; y: number } }[];
  edges: { source: string; sourceHandle: string; target: string; targetHandle: string; type: string }[];
}

let pastes = 0; // each paste of the same copy lands a step further

function clipOf(ids: string[]): Clip | null {
  const chosen = nodes.value.filter((n) => ids.includes(n.id) && !specs.value.get(n.data?.type ?? "")?.fixed);
  if (!chosen.length) return null;
  const taken = new Set(chosen.map((n) => n.id));
  return {
    nodes: chosen.map((n) => ({
      id: n.id,
      type: n.data!.type,
      config: JSON.parse(JSON.stringify(n.data!.config ?? {})),
      position: { x: n.position.x, y: n.position.y },
    })),
    edges: edges.value
      .filter((e) => taken.has(e.source) && taken.has(e.target)) // wires inside the copy only
      .map((e) => ({ source: e.source, sourceHandle: e.sourceHandle!, target: e.target, targetHandle: e.targetHandle!, type: e.data?.type ?? "" })),
  };
}

function place(clip: Clip, offset: number): string[] {
  const ids = new Map<string, string>();
  for (const n of clip.nodes) {
    const spec = specs.value.get(n.type);
    if (!spec || spec.fixed) continue; // not installed in this runtime
    const id = uniqueNodeId(n.type);
    ids.set(n.id, id);
    nodes.value = [
      ...nodes.value,
      { id, type: "fp", position: { x: n.position.x + offset, y: n.position.y + offset }, data: { type: n.type, config: n.config } },
    ];
  }
  const wires = clip.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => makeEdge(ids.get(e.source)!, e.sourceHandle, ids.get(e.target)!, e.targetHandle, e.type));
  if (wires.length) edges.value = [...edges.value, ...wires];
  return [...ids.values()];
}

/** Copy nodes (and the wires between them); how many were copied. */
export function copyNodes(ids: string[]): number {
  const clip = clipOf(ids);
  if (!clip) return 0;
  localStorage.setItem(CLIPBOARD_KEY, JSON.stringify(clip));
  pastes = 0;
  return clip.nodes.length;
}

/** Paste what was copied; the ids of the new nodes. */
export function pasteNodes(): string[] {
  let clip: Clip;
  try {
    clip = JSON.parse(localStorage.getItem(CLIPBOARD_KEY) ?? "null") as Clip;
  } catch {
    return [];
  }
  if (!clip?.nodes?.length) return [];
  pastes += 1;
  return place(clip, PASTE_STEP * pastes);
}

/** A copy of the nodes next to them, without touching the clipboard. */
export function duplicateNodes(ids: string[]): string[] {
  const clip = clipOf(ids);
  return clip ? place(clip, PASTE_STEP) : [];
}

export const SETTING_TYPE = "config.value/1";

/** The inputs a node has for the settings the graph made inputs of it. */
export function settingPorts(node: FpNode | undefined): PortInfo[] {
  const spec = node?.data ? specs.value.get(node.data.type) : undefined;
  return (node?.data?.setting_inputs ?? [])
    .filter((key) => spec?.config_schema.properties?.[key]?.["x-flowpilot-dynamic"])
    .map((key) => ({
      name: key,
      type: SETTING_TYPE,
      kind: "value" as const,
      required: false,
      delivery: "lossless" as const,
      description: "",
    }));
}

/** Make a setting an input of the node, or a plain setting again (its wire goes). */
export function toggleSettingInput(nodeId: string, key: string) {
  const node = nodes.value.find((n) => n.id === nodeId);
  if (!node?.data) return;
  const now = node.data.setting_inputs ?? [];
  const on = !now.includes(key);
  const setting_inputs = on ? [...now, key] : now.filter((k) => k !== key);
  nodes.value = nodes.value.map((n) =>
    n.id === nodeId ? { ...n, data: { ...n.data!, setting_inputs } } : n,
  );
  if (!on) edges.value = edges.value.filter((e) => !(e.target === nodeId && e.targetHandle === key));
}

export function removeNode(id: string) {
  const node = nodes.value.find((n) => n.id === id);
  if (node && specs.value.get(node.data?.type ?? "")?.fixed) return; // never deleted
  nodes.value = nodes.value.filter((n) => n.id !== id);
  edges.value = edges.value.filter((e) => e.source !== id && e.target !== id);
  if (selectedId.value === id) selectedId.value = null;
}

export function renameNode(oldId: string, newId: string): boolean {
  if (oldId === newId) return true;
  if (!/^[a-z0-9][a-z0-9_]*$/.test(newId) || nodes.value.some((n) => n.id === newId)) return false;
  nodes.value = nodes.value.map((n) => (n.id === oldId ? { ...n, id: newId } : n));
  edges.value = edges.value.map((e) => {
    const source = e.source === oldId ? newId : e.source;
    const target = e.target === oldId ? newId : e.target;
    return makeEdge(source, e.sourceHandle!, target, e.targetHandle!, e.data!.type);
  });
  selectedId.value = newId;
  return true;
}

// --- live debug --------------------------------------------------------------------------------

/**
 * Start a turn of the deployed graph from the editor (the Client input node) and watch it in live
 * debug, which this turns on first so debug taps see the turn.
 */
export async function startTurn(input: { text: string } | Blob | null) {
  const id = graphId.value;
  if (!id || turnStarting.value) return;
  turnStarting.value = true;
  try {
    if (!debug.enabled) {
      debug.enabled = true;
      trace?.setDebug(true);
    }
    await trace?.debugApplied();
    debug.selectedTurn = null; // follow the new turn
    await api.value!.turn(id, input);
  } catch (err) {
    flash((err as Error).message, "error");
  } finally {
    turnStarting.value = false;
  }
}

// The runtime runs the graph's debug taps only while someone has live debug on.
watch(
  () => debug.enabled,
  (on) => trace?.setDebug(on),
);

function onTrace(msg: TraceMsg | { type: string; graph?: string }) {
  if (msg.graph !== graphId.value) return;
  if (msg.type === "deployed") {
    debug.redeployed = true;
    return;
  }
  if (msg.type !== "trace") return;
  const ev = msg as TraceMsg;
  let turn = debug.turns.find((t) => t.id === ev.turn_id);
  if (!turn) {
    if (ev.event !== "turn.start") return; // backlog tail of a turn we never saw starting
    turn = { id: ev.turn_id, status: "running", nodes: {}, ports: {}, hostCalls: [], outputs: [], logs: {}, notes: {} };
    debug.turns.unshift(turn);
    debug.turns.splice(MAX_TURNS);
    turn = debug.turns[0];
  }
  const node = ev.node ?? "";
  switch (ev.event) {
    case "turn.end":
      turn.status = String(ev.attrs.status);
      turn.durationMs = ev.t_ms;
      break;
    case "node.start":
      turn.nodes[node] = { status: "running", startMs: ev.t_ms };
      break;
    case "node.end": {
      const run = turn.nodes[node] ?? { status: "running", startMs: 0 };
      run.status = String(ev.attrs.status);
      run.durationMs = Number(ev.attrs.duration_ms);
      if (ev.attrs.error) run.error = String(ev.attrs.error);
      turn.nodes[node] = run;
      break;
    }
    case "port.first_item": {
      const key = `${node}.${ev.attrs.port}`;
      turn.ports[key] ??= { items: 1, state: "flowing", firstMs: ev.t_ms };
      break;
    }
    case "port.end": {
      const key = `${node}.${ev.attrs.port}`;
      turn.ports[key] = {
        ...turn.ports[key],
        items: Number(ev.attrs.items),
        state: String(ev.attrs.state),
        endMs: ev.t_ms,
      };
      break;
    }
    case "log": {
      const { kind, summary, detail, ...attrs } = ev.attrs;
      const entries = (turn.logs[node] ??= []);
      if (!entries.some((e) => e.t === ev.t_ms && e.kind === kind)) {
        entries.push({
          t: ev.t_ms,
          kind: String(kind ?? ""),
          summary: String(summary ?? ""),
          ...(detail ? { detail: String(detail) } : {}),
          attrs,
        });
      }
      break;
    }
    case "host.call":
      turn.hostCalls.push({ node, op: String(ev.attrs.op), t: ev.t_ms });
      break;
    case "port.data": {
      const port = String(ev.attrs.port);
      if (turn.outputs.some((o) => o.node === node && o.port === port)) break; // a resent backlog
      const a = ev.attrs;
      const opt = <T,>(key: string, cast: (v: unknown) => T) => (a[key] != null ? { [key]: cast(a[key]) } : {});
      turn.outputs.push({
        node,
        port,
        type: String(a.type ?? ""),
        t: turn.ports[`${node}.${port}`]?.firstMs ?? ev.t_ms,
        items: Number(a.items ?? 0),
        state: String(a.state ?? "end"),
        ...opt("text", String),
        ...opt("partials", Number),
        ...opt("json", String),
        ...opt("more", Number),
        ...opt("file", String),
        ...opt("seconds", Number),
        ...opt("same", String),
        ...opt("error", String),
      });
      break;
    }
    default: {
      // a node's own event (audio.agc levels, vad.end, ...): shown on the node in live debug
      if (!node || ev.event.startsWith("turn.") || ev.event.startsWith("port.")) break;
      const notes = (turn.notes[node] ??= []);
      const same = (n: { event: string; attrs: TraceMsg["attrs"] }) =>
        n.event === ev.event && JSON.stringify(n.attrs) === JSON.stringify(ev.attrs);
      if (!notes.some(same)) notes.push({ event: ev.event, t: ev.t_ms, attrs: ev.attrs }); // a resent backlog
      break;
    }
  }
}
