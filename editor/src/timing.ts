// The selected turn's timings for the canvas and the debug panel (see flow.ts for what they mean).

import { computed } from "vue";
import { momentOf, nodeTimings, sinkOf, sourceOf, turnTiming, type NodeTiming, type TurnTiming, type Wire } from "./flow";
import { t } from "./i18n";
import { currentTurn, edges, nodes } from "./store";

export type { NodeTiming, TurnTiming };

const wires = computed<Wire[]>(() =>
  edges.value.map((e) => ({ source: e.source, sourceHandle: e.sourceHandle ?? "", target: e.target, targetHandle: e.targetHandle ?? "" })),
);

const types = computed<Record<string, string>>(() =>
  Object.fromEntries(nodes.value.map((n) => [n.id, String(n.data?.type ?? "")])),
);

/** The client's output node: it shows the turn's times instead of its own. */
export const sinkId = computed(() => sinkOf(types.value));

/** Timings of the selected turn's nodes, shared by the canvas and the debug panel. */
export const currentTimings = computed(() =>
  currentTurn.value ? nodeTimings(currentTurn.value, wires.value, sinkId.value) : ({} as Record<string, NodeTiming>),
);

/** The selected turn's times: the whole turn, to the first sound and text, the path to them. */
export const currentTurnTiming = computed<TurnTiming | undefined>(() =>
  currentTurn.value ? turnTiming(currentTurn.value, wires.value, sourceOf(types.value), sinkId.value) : undefined,
);

export function fmtMs(value: number): string {
  if (value >= 10000) return `${(value / 1000).toFixed(1)} s`;
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`;
}

/** A moment of the turn as the timeline's hover shows it: "1 850 ms". */
export function fmtAt(value: number): string {
  return `${Math.round(value).toLocaleString("ru-RU")} ms`;
}

/** What a node was doing at ``at`` (ms from the turn's start), as lines for the timeline's hover. */
export function describeMoment(id: string, at: number): string[] {
  const turn = currentTurn.value;
  if (!turn) return [];
  const m = momentOf(turn, wires.value, id, at);
  if (!m) return [];
  const lines = [
    m.state === "draining" && m.inEnd !== undefined
      ? t("moment.draining", { at: fmtAt(m.inEnd), over: fmtMs(Math.max(0, at - m.inEnd)) })
      : t(`moment.${m.state}`, { status: m.status, at: m.endMs !== undefined ? fmtAt(m.endMs) : "" }),
  ];
  for (const out of m.outputs) {
    if (out.first === undefined) continue;
    if (out.first <= at && (out.end === undefined || at < out.end)) {
      lines.push(t("moment.outFlowing", { port: out.port, at: fmtAt(out.first) }));
    } else if (out.end !== undefined && out.end <= at) {
      lines.push(t("moment.outDone", { port: out.port, at: fmtAt(out.end), n: out.items }));
    } else {
      lines.push(t("moment.outLater", { port: out.port, at: fmtAt(out.first) }));
    }
  }
  for (const input of m.inputs) {
    if (input.first === undefined) continue;
    if (at < input.first) lines.push(t("moment.inWaiting", { port: input.port, from: input.from, at: fmtAt(input.first) }));
    else if (input.end === undefined || at < input.end) lines.push(t("moment.inFlowing", { port: input.port, from: input.from, at: fmtAt(input.first) }));
    else lines.push(t("moment.inDone", { port: input.port, at: fmtAt(input.end) }));
  }
  if (m.host) lines.push(t("moment.host", { op: m.host.op, at: fmtAt(m.host.t) }));
  if (m.log) lines.push(t("moment.log", { text: m.log.summary, at: fmtAt(m.log.t) }));
  return lines;
}
