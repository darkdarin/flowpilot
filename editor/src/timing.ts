// Delays in a turn, from what flowed on the wires. In a streaming graph every node runs for the
// whole turn, so a node's run time says little; what tells where time goes is when data reached a
// node and when the node passed something on.

import { computed } from "vue";
import { currentTurn, edges, type FpEdge, type FpNode, type TurnRun } from "./store";

export interface NodeTiming {
  /** the first item on any input arrived (ms from the turn's start) */
  inFirst?: number;
  /** the last input ended */
  inEnd?: number;
  /** the node's first output item */
  outFirst?: number;
  /** the node's last output ended */
  outEnd?: number;
  /** first input → first output: how quickly the node answers */
  reaction?: number;
  /** last input ended → last output ended: what the node still did after it got everything */
  tail?: number;
}

export interface TurnTiming {
  /** the client's speech ended (voice turns) */
  speechEnd?: number;
  /** what the turn waited from: the end of speech, or the typed text */
  from?: number;
  /** the first text of the answer reached the client */
  firstText?: number;
  /** the first speech reached the client */
  firstSound?: number;
}

function minOf(values: (number | undefined)[]): number | undefined {
  const known = values.filter((v): v is number => v !== undefined);
  return known.length ? Math.min(...known) : undefined;
}

function maxOf(values: (number | undefined)[]): number | undefined {
  const known = values.filter((v): v is number => v !== undefined);
  return known.length ? Math.max(...known) : undefined;
}

/** Ports that carried something: their first item and end times. */
function flowed(turn: TurnRun, key: string): { first?: number; end?: number } {
  const port = turn.ports[key];
  if (!port || !port.items || port.firstMs === undefined) return {};
  return { first: port.firstMs, end: port.endMs };
}

export function nodeTimings(turn: TurnRun, edges: FpEdge[]): Record<string, NodeTiming> {
  const result: Record<string, NodeTiming> = {};
  for (const id of Object.keys(turn.nodes)) {
    const ins = edges.filter((e) => e.target === id).map((e) => flowed(turn, `${e.source}.${e.sourceHandle}`));
    const outs = Object.keys(turn.ports)
      .filter((key) => key.startsWith(`${id}.`))
      .map((key) => flowed(turn, key));
    const timing: NodeTiming = {
      inFirst: minOf(ins.map((p) => p.first)),
      inEnd: maxOf(ins.map((p) => p.end)),
      outFirst: minOf(outs.map((p) => p.first)),
      outEnd: maxOf(outs.map((p) => p.end)),
    };
    if (timing.inFirst !== undefined && timing.outFirst !== undefined) {
      timing.reaction = Math.max(0, timing.outFirst - timing.inFirst);
    }
    if (timing.inEnd !== undefined && timing.outEnd !== undefined && timing.outEnd >= timing.inEnd) {
      timing.tail = timing.outEnd - timing.inEnd;
    }
    result[id] = timing;
  }
  return result;
}

/** Timings of the selected turn's nodes, shared by the canvas and the debug panel. */
export const currentTimings = computed(() =>
  currentTurn.value ? nodeTimings(currentTurn.value, edges.value) : ({} as Record<string, NodeTiming>),
);

export function turnTiming(turn: TurnRun, nodes: FpNode[], edges: FpEdge[]): TurnTiming {
  const typeOf = (id: string) => nodes.find((n) => n.id === id)?.data?.type;
  const source = nodes.find((n) => n.data?.type === "client.source")?.id;
  const sink = nodes.find((n) => n.data?.type === "client.sink")?.id;
  const timing: TurnTiming = {};
  if (source) {
    timing.speechEnd = flowed(turn, `${source}.audio`).end;
    timing.from = timing.speechEnd ?? flowed(turn, `${source}.utterance`).first;
  }
  if (sink) {
    const into = (port: string) =>
      minOf(
        edges
          .filter((e) => e.target === sink && e.targetHandle === port && typeOf(e.source))
          .map((e) => flowed(turn, `${e.source}.${e.sourceHandle}`).first),
      );
    timing.firstText = minOf([into("delta"), into("response")]);
    timing.firstSound = into("speech");
  }
  return timing;
}

export function fmtMs(value: number): string {
  if (value >= 10000) return `${(value / 1000).toFixed(1)} s`;
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`;
}
