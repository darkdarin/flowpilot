// Typed client for the runtime's editor API (see flowpilot_server/editor_api.py, tracehub.py).

export interface PortInfo {
  name: string;
  type: string;
  kind: "stream" | "value";
  required: boolean;
  delivery: "lossless" | "latest";
  description: string;
}

/** Texts of a node in one language; every key is optional and falls back to English. */
export interface NodeTexts {
  title?: string;
  description?: string;
  inputs?: Record<string, string>;
  outputs?: Record<string, string>;
  config?: Record<string, { title?: string; description?: string }>;
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
}

export interface Catalog {
  nodes: NodeInfo[];
  types: { id: string; privileged: boolean }[];
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
};

export interface HostOption {
  id: string;
  name: string;
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
    const resp = await fetch(`${ROOT}/api/editor${path}`, {
      method,
      headers: {
        ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
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
      if (msg.type === "hello.ok") {
        this.onState(true);
        if (this.graph) this.subscribe(this.graph);
      } else this.onMessage(msg);
    };
    ws.onclose = () => {
      this.onState(false);
      if (!this.closed) setTimeout(() => this.open(), 2000);
    };
  }

  subscribe(graph: string) {
    this.graph = graph;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "subscribe", graph }));
  }

  close() {
    this.closed = true;
    this.ws?.close();
  }
}
