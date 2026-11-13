// Where the time of a turn went, from what flowed on the wires. Pure: no Vue, no store, so it can be
// checked on its own against a recorded turn.
//
// In a streaming graph every node runs for the whole turn, so a node's run time says little. What
// tells where time goes is when data reached a node and when the node passed something on:
//
// - reaction: its first input → its first output (how quickly it starts answering);
// - delay: the last input it waited for → its first output (the time it added itself);
// - the path to the answer: from the client's first sound (or text) back to the turn's source,
//   each node through the input it waited for last. The delays along it add up to the time to the
//   first sound: the nodes on it are the ones to make faster.

export interface PortFlow {
  items: number;
  state?: string;
  firstMs?: number;
  endMs?: number;
}

export interface RunInfo {
  status: string;
  startMs: number;
  durationMs?: number;
  error?: string;
}

export interface FlowTurn {
  durationMs?: number;
  nodes: Record<string, RunInfo>;
  ports: Record<string, PortFlow>;
  hostCalls: { node: string; op: string; t: number }[];
  logs: Record<string, { t: number; kind: string; summary: string }[]>;
}

export interface Wire {
  source: string;
  sourceHandle: string;
  target: string;
  targetHandle: string;
}

export interface NodeTiming {
  /** the first item on any input arrived (ms from the turn's start) */
  inFirst?: number;
  /** the last input ended */
  inEnd?: number;
  /** the node's first output item */
  outFirst?: number;
  /** the node's last output ended */
  outEnd?: number;
  /** first input → first output: how quickly the node starts answering */
  reaction?: number;
  /** the last input it waited for → its first output (on the path: that output): its own time */
  delay?: number;
  /** the input it waited for last */
  waitedFor?: string;
  /** on the path to the client's first sound (or text): its delay is part of the answer's */
  onPath?: boolean;
}

export interface PathStep {
  node: string;
  /** the output that carried the answer on */
  port: string;
  /** when that output gave its first item */
  at: number;
  /** what the node added: from the input it waited for last (from its start, for the source) */
  delay: number;
  waitedFor?: string;
}

export interface TurnTiming {
  /** the whole turn */
  total?: number;
  /** the client's speech ended (voice turns) */
  speechEnd?: number;
  /** the first text of the answer reached the client */
  firstText?: number;
  /** the first sound of the answer reached the client */
  firstSound?: number;
  /** what the person waited for: the first sound, else the first text (from the turn's start) */
  effective?: number;
  /** the path to that first sound or text, from the source to the client */
  path: PathStep[];
}

const SOUND_PORTS = ["speech", "ack"];
const TEXT_PORTS = ["delta", "response"];

interface Flowed {
  first: number;
  end?: number;
}

function flowed(turn: FlowTurn, key: string): Flowed | undefined {
  const port = turn.ports[key];
  if (!port || !port.items || port.firstMs === undefined) return undefined;
  return { first: port.firstMs, end: port.endMs };
}

function minOf(values: (number | undefined)[]): number | undefined {
  const known = values.filter((v): v is number => v !== undefined);
  return known.length ? Math.min(...known) : undefined;
}

function maxOf(values: (number | undefined)[]): number | undefined {
  const known = values.filter((v): v is number => v !== undefined);
  return known.length ? Math.max(...known) : undefined;
}

/** The inputs of a node that carried something, with when. */
export function inputsOf(turn: FlowTurn, wires: Wire[], id: string) {
  return wires
    .filter((w) => w.target === id)
    .map((w) => ({ port: w.targetHandle, from: `${w.source}.${w.sourceHandle}`, wire: w, flow: flowed(turn, `${w.source}.${w.sourceHandle}`) }));
}

/** The outputs of a node that carried something, with when. */
export function outputsOf(turn: FlowTurn, id: string) {
  return Object.keys(turn.ports)
    .filter((key) => key.startsWith(`${id}.`))
    .map((key) => ({ port: key.slice(id.length + 1), items: turn.ports[key].items, flow: flowed(turn, key) }));
}

/** The input a node waited for last before ``at``: the latest first item that came by then. */
function lastAwaited(turn: FlowTurn, wires: Wire[], id: string, at: number) {
  let best: { port: string; wire: Wire; first: number } | undefined;
  for (const input of inputsOf(turn, wires, id)) {
    if (!input.flow || input.flow.first > at) continue;
    if (!best || input.flow.first > best.first) best = { port: input.port, wire: input.wire, first: input.flow.first };
  }
  return best;
}

export function sinkOf(types: Record<string, string>): string | undefined {
  return Object.keys(types).find((id) => types[id] === "client.sink");
}

export function sourceOf(types: Record<string, string>): string | undefined {
  return Object.keys(types).find((id) => types[id] === "client.source");
}

/** The path to the client's first sound (else text), from the source to the client. */
export function answerPath(turn: FlowTurn, wires: Wire[], sink: string): PathStep[] {
  const into = (ports: string[]) =>
    wires
      .filter((w) => w.target === sink && ports.includes(w.targetHandle))
      .map((w) => ({ wire: w, flow: flowed(turn, `${w.source}.${w.sourceHandle}`) }))
      .filter((x): x is { wire: Wire; flow: Flowed } => x.flow !== undefined)
      .sort((a, b) => a.flow.first - b.flow.first)[0];
  const start = into(SOUND_PORTS) ?? into(TEXT_PORTS);
  if (!start) return [];
  const steps: PathStep[] = [];
  let node = start.wire.source;
  let port = start.wire.sourceHandle;
  let at = start.flow.first;
  const seen = new Set<string>();
  while (!seen.has(node)) {
    seen.add(node);
    const waited = lastAwaited(turn, wires, node, at);
    if (!waited) {
      steps.push({ node, port, at, delay: Math.max(0, at - (turn.nodes[node]?.startMs ?? 0)) });
      break;
    }
    steps.push({ node, port, at, delay: Math.max(0, at - waited.first), waitedFor: waited.port });
    node = waited.wire.source;
    port = waited.wire.sourceHandle;
    at = waited.first;
  }
  return steps.reverse();
}

export function nodeTimings(turn: FlowTurn, wires: Wire[], sink?: string): Record<string, NodeTiming> {
  const path = sink ? answerPath(turn, wires, sink) : [];
  const result: Record<string, NodeTiming> = {};
  for (const id of Object.keys(turn.nodes)) {
    const ins = inputsOf(turn, wires, id).map((i) => i.flow);
    const outs = outputsOf(turn, id).map((o) => o.flow);
    const timing: NodeTiming = {
      inFirst: minOf(ins.map((p) => p?.first)),
      inEnd: maxOf(ins.map((p) => p?.end)),
      outFirst: minOf(outs.map((p) => p?.first)),
      outEnd: maxOf(outs.map((p) => p?.end)),
    };
    if (timing.outFirst !== undefined) {
      if (timing.inFirst !== undefined) timing.reaction = Math.max(0, timing.outFirst - timing.inFirst);
      const waited = lastAwaited(turn, wires, id, timing.outFirst);
      if (waited) {
        timing.delay = Math.max(0, timing.outFirst - waited.first);
        timing.waitedFor = waited.port;
      } else if (!ins.some(Boolean)) {
        timing.delay = Math.max(0, timing.outFirst - turn.nodes[id].startMs);
      }
    }
    result[id] = timing;
  }
  for (const step of path) {
    const timing = (result[step.node] ??= {});
    timing.onPath = true;
    timing.delay = step.delay; // through the output that carried the answer on
    timing.waitedFor = step.waitedFor;
  }
  return result;
}

export function turnTiming(turn: FlowTurn, wires: Wire[], source?: string, sink?: string): TurnTiming {
  const timing: TurnTiming = { total: turn.durationMs, path: [] };
  if (source) timing.speechEnd = flowed(turn, `${source}.audio`)?.end;
  if (sink) {
    const into = (ports: string[]) =>
      minOf(
        wires
          .filter((w) => w.target === sink && ports.includes(w.targetHandle))
          .map((w) => flowed(turn, `${w.source}.${w.sourceHandle}`)?.first),
      );
    timing.firstText = into(TEXT_PORTS);
    timing.firstSound = into(SOUND_PORTS);
    timing.effective = timing.firstSound ?? timing.firstText;
    timing.path = answerPath(turn, wires, sink);
  }
  return timing;
}

/** What a node was doing at one moment of the turn, for the timeline's hover. */
export interface Moment {
  /** sending: passing data on while its input still comes; draining: every input has ended and
   * it still passes data on (time of its own, past its input) */
  state: "idle" | "waiting" | "working" | "sending" | "draining" | "finishing" | "done" | "failed";
  status: string;
  startMs: number;
  endMs?: number;
  /** the last of its inputs ended */
  inEnd?: number;
  inputs: { port: string; from: string; first?: number; end?: number }[];
  outputs: { port: string; items: number; first?: number; end?: number }[];
  log?: { t: number; kind: string; summary: string };
  host?: { t: number; op: string };
}

export function momentOf(turn: FlowTurn, wires: Wire[], id: string, t: number): Moment | undefined {
  const run = turn.nodes[id];
  if (!run) return undefined;
  const endMs = run.durationMs !== undefined ? run.startMs + run.durationMs : undefined;
  const inputs = inputsOf(turn, wires, id).map((i) => ({ port: i.port, from: i.from, first: i.flow?.first, end: i.flow?.end }));
  const outputs = outputsOf(turn, id).map((o) => ({ port: o.port, items: o.items, first: o.flow?.first, end: o.flow?.end }));
  const before = <T extends { t: number }>(list: T[]) => list.filter((e) => e.t <= t).at(-1);
  const flowedIn = inputs.filter((i) => i.first !== undefined);
  const inEnd = flowedIn.length && flowedIn.every((i) => i.end !== undefined) ? Math.max(...flowedIn.map((i) => i.end!)) : undefined;
  let state: Moment["state"];
  if (t < run.startMs) state = "idle";
  else if (endMs !== undefined && t >= endMs) state = ["ok", "running"].includes(run.status) ? "done" : "failed";
  else if (outputs.some((o) => o.first !== undefined && o.first <= t && (o.end === undefined || t < o.end))) {
    state = inEnd !== undefined && inEnd <= t ? "draining" : "sending";
  } else if (outputs.some((o) => o.first !== undefined && o.first <= t)) state = "finishing";
  else if (inputs.some((i) => i.first !== undefined && i.first <= t)) state = "working";
  else state = "waiting";
  return {
    state,
    status: run.status,
    startMs: run.startMs,
    endMs,
    inEnd,
    inputs,
    outputs,
    log: before(turn.logs[id] ?? []),
    host: before(turn.hostCalls.filter((c) => c.node === id)),
  };
}
