// Typed client for the runtime's editor API (see flowpilot_server/editor_api.py, tracehub.py).

export interface PortInfo {
  name: string;
  type: string;
  kind: "stream" | "value";
  required: boolean;
  delivery: "lossless" | "latest";
  description: string;
  /** a type variable: ports of a node with the same one carry one type */
  var?: string;
}

/** Texts of a node in one language; every key is optional and falls back to English. */
export interface NodeTexts {
  title?: string;
  description?: string;
  inputs?: Record<string, string>;
  outputs?: Record<string, string>;
  config?: Record<string, { title?: string; description?: string; options?: Record<string, string> }>;
  /** texts of setting groups (x-flowpilot-groups) */
  groups?: Record<string, { title?: string; description?: string }>;
}

export interface NodeInfo {
  type: string;
  version: string;
  title: string;
  category: string;
  description: string;
  inputs: PortInfo[];
  outputs: PortInfo[];
  config_schema: JsonSchema;
  host_ops: string[];
  grants: string[];
  turn_timeout_s: number;
  translations: Record<string, NodeTexts>;
  /** a debugging tap: runs only while someone watches the graph in live debug */
  debug_tap?: boolean;
  /** every graph has exactly one: created with the graph, not in the palette, not deletable */
  fixed?: boolean;
}

export interface Catalog {
  nodes: NodeInfo[];
  types: { id: string; privileged: boolean; kind?: "stream" | "value" }[];
}

export type JsonSchema = {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  enum?: unknown[];
  default?: unknown;
  description?: string;
  title?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  pattern?: string;
  minLength?: number;
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  /** names something a host provides, e.g. "ha.stt" (see OPTIONS_KEY in the SDK) */
  "x-flowpilot-options"?: string;
  /** the node lists the choices itself from the rest of its config (POST /node-options) */
  "x-flowpilot-node-options"?: boolean;
  /** English labels of an enum's values (translations: config.<setting>.options) */
  "x-flowpilot-enum-titles"?: Record<string, string>;
  /** the group this setting belongs to; editors show a group as one entry opening its settings */
  "x-flowpilot-group"?: string;
  /** a node's setting groups with their English texts (on the config schema itself) */
  "x-flowpilot-groups"?: Record<string, { title?: string; description?: string }>;
  /** applies only to what one input carries (WHEN_INPUT_KEY in the SDK); "*": anything connected */
  "x-flowpilot-when-input"?: { port: string; types: string[] | "*" };
  /** the graph may make this setting an input of the node, for one turn at a time */
  "x-flowpilot-dynamic"?: boolean;
  uniqueItems?: boolean;
  format?: string;
};

export interface HostOption {
  id: string;
  name: string;
  description?: string;
}

export interface HostOptions {
  available: boolean;
  hosts: string[];
  options: Record<string, HostOption[]>;
  /** kinds whose lists are complete: anything else cannot work */
  strict: string[];
}

export interface GraphNode {
  id: string;
  type: string;
  version?: string;
  config?: Record<string, unknown>;
  /** settings made inputs of the node (x-flowpilot-dynamic): an input of each name */
  setting_inputs?: string[];
}

export interface GraphEdge {
  from: string;
  to: string;
}

export interface GraphDoc {
  schema_version: number;
  id: string;
  name?: string;
  settings?: Record<string, unknown>;
  nodes: GraphNode[];
  edges: GraphEdge[];
  ui?: { nodes?: Record<string, { x: number; y: number }> } & Record<string, unknown>;
}

export interface Issue {
  code: string;
  path: string;
  message: string;
  node?: string;
  edge?: number;
  field?: string;
}

/** A runtime secret: never its value, only whether it is set and which graphs use it. */
export interface SecretInfo {
  name: string;
  set: boolean;
  graphs: string[];
}

/** A skill: a tool for the LLM agent, described by the owner on top of Home Assistant's actions. */
export interface SkillField {
  name: string;
  title: string;
  type: "text" | "number" | "choice";
  options: string[];
  required: boolean;
}

export interface SkillAction {
  action: string;
  data: Record<string, unknown>;
  target: Record<string, unknown>;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  fields: SkillField[];
  actions: SkillAction[];
  enabled: boolean;
}

/** A quick command people taught: phrases and the Home Assistant tool calls they replay. */
export interface QuickCommand {
  id: string;
  phrases: string[];
  actions: { tool: string; arguments: Record<string, unknown> }[];
  reply: string;
  created_at: string;
  created_by: string | null;
  source: string;
  uses: number;
}

export interface CommandList {
  commands: QuickCommand[];
  people: { id: string; name: string }[];
}

/** A fact of the long-term memory: the home's (``owner`` null) or one person's. */
export interface Fact {
  id: string;
  text: string;
  owner: string | null;
  created_at: string;
  source: string;
}

export interface MemoryList {
  facts: Fact[];
  /** Home Assistant users facts may belong to (from the connected host). */
  people: { id: string; name: string }[];
  people_known: boolean;
}

export interface GraphSummary {
  id: string;
  name: string;
  deployed: boolean;
  has_draft: boolean;
  default: boolean;
}

export interface GraphDetail {
  id: string;
  deployed: GraphDoc | null;
  draft: GraphDoc | null;
  live: boolean;
  issues: Issue[];
  warnings: Issue[];
}

export interface TraceMsg {
  type: "trace";
  graph: string;
  turn_id: string;
  event: string;
  node: string | null;
  t_ms: number;
  attrs: Record<string, string | number | boolean | null>;
}

/**
 * Where the runtime is mounted: "" normally, "/api/hassio_ingress/<token>" when Home Assistant
 * serves the editor through Ingress. The editor itself always lives under "<root>/editor/".
 */
export const ROOT = location.pathname.replace(/\/editor(\/.*)?$/, "");

/** A debug recording kept by a Debug probe. */
export interface Recording {
  file: string;
  graph: string;
  node: string;
  turn_id: string;
  created: string;
  seconds: number;
  bytes: number;
}

/** A sample of a person's voice: its embedding stays on the runtime, the audio is not kept. */
export interface VoiceSample {
  id: string;
  model: string;
  source: "microphone" | "recording" | string;
  seconds: number;
  level_db: number;
  created: string;
  current: boolean; // of the model in use
}

export interface VoiceSettings {
  model: string;
  threshold: number;
  default_threshold: number;
  margin: number;
  models: { id: string; title: string; ready: boolean }[];
}

export interface Voices extends VoiceSettings {
  profiles: Record<string, VoiceSample[]>;
  people: HostOption[];
}

export interface VoiceTest {
  scores: Record<string, number>;
  match: string | null;
  status: string;
  reason: string | null;
  threshold: number;
  margin: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly issues: Issue[] = [],
  ) {
    super(message);
  }
}

export class Api {
  constructor(public token: string) {}

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const raw = body instanceof Blob;
    const resp = await fetch(`${ROOT}/api/editor${path}`, {
      method,
      headers: {
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        ...(body === undefined ? {} : { "Content-Type": raw ? body.type || "application/octet-stream" : "application/json" }),
      },
      body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
    });
    const text = await resp.text();
    let data: unknown = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { error: text };
    }
    if (!resp.ok) {
      const err = (data ?? {}) as { error?: string; issues?: Issue[] };
      throw new ApiError(resp.status, err.error ?? resp.statusText, err.issues ?? []);
    }
    return data as T;
  }

  catalog() {
    return this.call<Catalog>("GET", "/catalog");
  }
  options(refresh = false) {
    return this.call<HostOptions>("GET", `/options${refresh ? "?refresh=1" : ""}`);
  }
  nodeOptions(type: string, field: string, config: Record<string, unknown>) {
    return this.call<{ options: HostOption[]; error?: string }>("POST", "/node-options", { type, field, config });
  }
  /** A recording as a Blob (an <audio> element cannot send the Authorization header). */
  async recording(file: string): Promise<Blob> {
    const resp = await fetch(`${ROOT}/api/editor/recordings/${encodeURIComponent(file)}`, {
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
    });
    if (!resp.ok) throw new ApiError(resp.status, resp.statusText);
    return resp.blob();
  }
  recordings() {
    return this.call<{ recordings: Recording[] }>("GET", "/recordings");
  }
  voices() {
    return this.call<Voices>("GET", "/voices");
  }
  /** ``threshold: null`` goes back to the model's own. */
  setVoices(settings: { model?: string; threshold?: number | null; margin?: number }) {
    return this.call<VoiceSettings>("PUT", "/voices", settings);
  }
  downloadVoiceModel() {
    return this.call<VoiceSettings>("POST", "/voices/model");
  }
  /** A sample of a person's voice: a WAV of them speaking, or a debug recording by name. */
  addVoiceSample(user: string, speech: Blob | { recording: string }) {
    return this.call<VoiceSample>("POST", `/voices/${encodeURIComponent(user)}/samples`, speech);
  }
  deleteVoiceSample(user: string, id: string) {
    return this.call<{ removed: boolean }>("DELETE", `/voices/${encodeURIComponent(user)}/samples/${encodeURIComponent(id)}`);
  }
  deleteVoice(user: string) {
    return this.call<{ removed: boolean }>("DELETE", `/voices/${encodeURIComponent(user)}`);
  }
  testVoice(speech: Blob | { recording: string }) {
    return this.call<VoiceTest>("POST", "/voices/test", speech);
  }
  deleteRecording(file: string) {
    return this.call<{ ok: boolean }>("DELETE", `/recordings/${encodeURIComponent(file)}`);
  }
  /**
   * Start a turn of the deployed graph, as a client would: text, a WAV recording, or nothing (only
   * nodes that speak by themselves act).
   */
  async turn(graph: string, input: { text: string } | Blob | null): Promise<{ status: string }> {
    const query = new URLSearchParams({ graph });
    const resp = await fetch(`${ROOT}/api/turn?${query}`, {
      method: "POST",
      headers: {
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        "Content-Type": input instanceof Blob ? "audio/wav" : "application/json",
      },
      body: input instanceof Blob ? input : JSON.stringify(input ?? {}),
    });
    const data = await resp.json().catch(() => ({ error: resp.statusText }));
    if (!resp.ok) throw new ApiError(resp.status, data.error ?? resp.statusText);
    return data;
  }
  secrets() {
    return this.call<{ secrets: SecretInfo[] }>("GET", "/secrets");
  }
  /** Set a secret; graphs using it redeploy: ``{graph: null | error}``. */
  setSecret(name: string, value: string) {
    return this.call<{ redeployed: Record<string, string | null> }>("PUT", `/secrets/${encodeURIComponent(name)}`, { value });
  }
  deleteSecret(name: string) {
    return this.call<{ redeployed: Record<string, string | null> }>("DELETE", `/secrets/${encodeURIComponent(name)}`);
  }
  skills() {
    return this.call<{ skills: Skill[] }>("GET", "/skills");
  }
  saveSkill(skill: Omit<Skill, "id"> & { id?: string }) {
    return skill.id
      ? this.call<Skill>("PUT", `/skills/${encodeURIComponent(skill.id)}`, skill)
      : this.call<Skill>("POST", "/skills", skill);
  }
  deleteSkill(id: string) {
    return this.call<{ removed: boolean }>("DELETE", `/skills/${encodeURIComponent(id)}`);
  }
  commands() {
    return this.call<CommandList>("GET", "/commands");
  }
  updateCommand(id: string, phrases: string[], reply: string) {
    return this.call<QuickCommand>("PUT", `/commands/${encodeURIComponent(id)}`, { phrases, reply });
  }
  deleteCommand(id: string) {
    return this.call<{ removed: boolean }>("DELETE", `/commands/${encodeURIComponent(id)}`);
  }
  memory() {
    return this.call<MemoryList>("GET", "/memory");
  }
  addFact(text: string, owner: string | null) {
    return this.call<Fact>("POST", "/memory", { text, owner });
  }
  updateFact(id: string, text: string, owner: string | null) {
    return this.call<Fact>("PUT", `/memory/${encodeURIComponent(id)}`, { text, owner });
  }
  deleteFact(id: string) {
    return this.call<{ removed: boolean }>("DELETE", `/memory/${encodeURIComponent(id)}`);
  }
  setDefault(graph: string) {
    return this.call<{ default: string }>("PUT", "/default", { graph });
  }
  graphs() {
    return this.call<{ graphs: GraphSummary[] }>("GET", "/graphs");
  }
  graph(id: string) {
    return this.call<GraphDetail>("GET", `/graphs/${id}`);
  }
  create(id: string, name: string) {
    return this.call<{ id: string; draft: GraphDoc; issues: Issue[]; warnings: Issue[] }>("POST", "/graphs", { id, name });
  }
  saveDraft(id: string, doc: GraphDoc) {
    return this.call<{ issues: Issue[]; warnings: Issue[] }>("PUT", `/graphs/${id}/draft`, doc);
  }
  discardDraft(id: string) {
    return this.call<{ ok: boolean }>("DELETE", `/graphs/${id}/draft`);
  }
  deploy(id: string) {
    return this.call<{ deployed: boolean }>("POST", `/graphs/${id}/deploy`);
  }
}

/** Live trace socket; reconnects until closed. */
export class TraceSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private graph: string | null = null;
  private ready = false; // hello.ok received on the current socket
  private sent: string | null = null; // graph subscribed on the current socket
  private debug = false; // live debug is on: the graph's debug taps run while it is
  private applied: Promise<void> = Promise.resolve(); // the runtime confirmed the last debug switch
  private confirm: (() => void) | null = null;

  constructor(
    private token: string,
    private onMessage: (msg: TraceMsg | { type: string; graph?: string }) => void,
    private onState: (connected: boolean) => void,
  ) {
    this.open();
  }

  private open() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}${ROOT}/api/editor/ws`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "hello", token: this.token }));
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data as string);
      if (msg.type === "debug") {
        this.confirmed();
      } else if (msg.type === "hello.ok") {
        this.ready = true;
        this.sent = null;
        this.onState(true);
        if (this.graph) this.subscribe(this.graph);
      } else this.onMessage(msg);
    };
    ws.onclose = () => {
      this.confirmed();
      this.ready = false;
      this.sent = null;
      this.onState(false);
      if (!this.closed) setTimeout(() => this.open(), 2000);
    };
  }

  subscribe(graph: string) {
    this.graph = graph;
    // Only after hello.ok, and once per graph: every subscribe makes the server resend its backlog.
    if (!this.ready || this.sent === graph) return;
    this.sent = graph;
    this.ws?.send(JSON.stringify({ type: "subscribe", graph, debug: this.debug }));
  }

  /** Live debug on or off for the subscribed graph (it switches the graph's debug taps). */
  setDebug(on: boolean) {
    if (this.debug === on) return;
    this.debug = on;
    if (!this.ready || !this.sent) return; // the next subscribe carries it
    this.confirmed();
    this.applied = new Promise((resolve) => (this.confirm = resolve));
    this.ws?.send(JSON.stringify({ type: "debug", on }));
  }

  /** Resolves once the runtime has applied the last live debug switch (its debug taps run). */
  debugApplied(): Promise<void> {
    return this.applied;
  }

  private confirmed() {
    this.confirm?.();
    this.confirm = null;
  }

  close() {
    this.closed = true;
    this.ws?.close();
  }
}
