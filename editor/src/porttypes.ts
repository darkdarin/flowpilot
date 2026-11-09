// Port types in the editor, as the runtime's validation sees them (flowpilot_core.validate):
// a port takes one type, a union ("a/1|b/1") or any type ("*", inputs only); ports of a node with the same `var` carry one
// type, the one connected to its inputs, and an output with it gives that type.

import { computed } from "vue";
import type { PortInfo } from "./api";
import { catalog, edges, nodes, settingPorts, specs, type FpEdge, type FpNode } from "./store";

/** A port type that takes every payload type (flowpilot_sdk.ANY). */
export const ANY = "*";

export function members(type: string): string[] {
  if (type === ANY) return catalog.value?.types.map((t) => t.id) ?? [];
  return type.split("|");
}

export function isUnion(port: PortInfo): boolean {
  return port.type === ANY || port.type.includes("|");
}

/** How a payload type travels when a union port carries it. */
function kindOf(type: string): string | undefined {
  return catalog.value?.types.find((t) => t.id === type)?.kind;
}

function portOf(nodeList: FpNode[], nodeId: string, port: string | null | undefined, side: "inputs" | "outputs") {
  const node = nodeList.find((n) => n.id === nodeId);
  const type = node?.data?.type;
  const own = type ? specs.value.get(type)?.[side].find((p) => p.name === port) : undefined;
  return own ?? (side === "inputs" ? settingPorts(node).find((p) => p.name === port) : undefined);
}

interface Resolution {
  /** `${node}|${var}` → the type bound */
  bindings: Map<string, string>;
}

function resolve(edgeList: FpEdge[], nodeList: FpNode[]): Resolution {
  const bindings = new Map<string, string>();
  const wired = edgeList
    .map((e) => ({ e, src: portOf(nodeList, e.source, e.sourceHandle, "outputs"), dst: portOf(nodeList, e.target, e.targetHandle, "inputs") }))
    .filter((w): w is { e: FpEdge; src: PortInfo; dst: PortInfo } => !!w.src && !!w.dst);
  let changed = true;
  while (changed) {
    changed = false;
    for (const { e, src, dst } of wired) {
      const given = carried(bindings, e.source, src);
      const key = `${e.target}|${dst.var}`;
      if (given && dst.var && !bindings.has(key) && members(dst.type).includes(given)) {
        bindings.set(key, given);
        changed = true;
      }
    }
  }
  return { bindings };
}

function carried(bindings: Map<string, string>, node: string, port: PortInfo): string | undefined {
  if (port.var) return bindings.get(`${node}|${port.var}`);
  return isUnion(port) ? undefined : port.type;
}

const resolution = computed(() => resolve(edges.value, nodes.value));

/** The type a port carries in this graph: the one resolved, else what the port declares. */
export function portType(nodeId: string, port: PortInfo, side: "inputs" | "outputs"): string {
  const { bindings } = resolution.value;
  if (port.var) return bindings.get(`${nodeId}|${port.var}`) ?? port.type;
  if (side === "inputs" && isUnion(port)) {
    const wire = edges.value.find((e) => e.target === nodeId && e.targetHandle === port.name);
    if (wire) {
      const src = portOf(nodes.value, wire.source, wire.sourceHandle, "outputs");
      if (src) return carried(bindings, wire.source, src) ?? port.type;
    }
  }
  return port.type;
}

/** The type a wire carries: what its output gives in this graph. */
export function edgeType(edge: FpEdge): string {
  const src = portOf(nodes.value, edge.source, edge.sourceHandle, "outputs");
  return src ? portType(edge.source, src, "outputs") : (edge.data?.type ?? "");
}

/** Whether an output may be wired to an input: the reason when not, else null. */
export function connectProblem(source: string, sourceHandle: string, target: string, targetHandle: string): string | null {
  const src = portOf(nodes.value, source, sourceHandle, "outputs");
  const dst = portOf(nodes.value, target, targetHandle, "inputs");
  if (!src || !dst || source === target) return "ports";
  // as if this input's old wire were gone: the new one replaces it
  const others = edges.value.filter((e) => !(e.target === target && e.targetHandle === targetHandle));
  const { bindings } = resolve(others, nodes.value);
  const given = carried(bindings, source, src);
  if (src.var && !given) return "unresolved";
  const types = given ? [given] : members(src.type);
  const accepted = members(dst.type);
  if (dst.type !== ANY && !types.every((t) => accepted.includes(t))) return "type";
  const bound = dst.var ? bindings.get(`${target}|${dst.var}`) : undefined;
  if (bound && given && bound !== given) return "generic";
  const srcKind = isUnion(src) ? (given ? kindOf(given) : undefined) : src.kind;
  const dstKind = isUnion(dst) ? (given ? kindOf(given) : undefined) : dst.kind;
  if (srcKind && dstKind && srcKind !== dstKind) return "kind";
  return null;
}

/** A port's kind in this graph: a union port has the kind of the type connected. */
export function portKind(nodeId: string, port: PortInfo, side: "inputs" | "outputs"): string {
  if (!isUnion(port)) return port.kind;
  const type = portType(nodeId, port, side);
  return type === ANY || type.includes("|") ? "any" : (kindOf(type) ?? port.kind);
}

/**
 * For a wire dragged from a port into empty space: the port of a new node of `spec` it would
 * connect to, if any. From an output (`side` "outputs") a new node's input takes what the output
 * gives; from an input, a new node's output must give what the input takes. A generic output of a
 * new node gives nothing yet (no input of it is connected), so it is not offered.
 */
export function fittingPort(nodeId: string, handle: string, side: "inputs" | "outputs", spec: { inputs: PortInfo[]; outputs: PortInfo[] }): PortInfo | undefined {
  const own = portOf(nodes.value, nodeId, handle, side);
  if (!own) return undefined;
  const kind = (port: PortInfo, types: string[]) =>
    isUnion(port) ? (types.length === 1 ? kindOf(types[0]) : undefined) : port.kind;
  if (side === "outputs") {
    const given = portType(nodeId, own, "outputs");
    if (own.var && given === own.type && isUnion(own)) return undefined; // nothing gives it a type yet
    const types = members(given);
    const ownKind = kind(own, types);
    return spec.inputs.find((p) => {
      if (p.type !== ANY && !types.every((t) => members(p.type).includes(t))) return false;
      const theirs = kind(p, types);
      return !ownKind || !theirs || ownKind === theirs;
    });
  }
  // the wire on this input (if any) is replaced; a generic input still takes its node's type
  const accepted = own.type === ANY ? null : members(own.var ? portType(nodeId, own, "inputs") : own.type);
  return spec.outputs.find((p) => {
    if (p.var) return false;
    const types = members(p.type);
    if (accepted && !types.every((t) => accepted.includes(t))) return false;
    const ours = kind(own, types);
    const theirs = kind(p, types);
    return !ours || !theirs || ours === theirs;
  });
}
