// Localized node texts from the catalog: the UI language, then its base language, then English
// (the English text lives in the spec itself).

import type { NodeInfo } from "./api";
import { locale } from "./i18n";

function pick(node: NodeInfo | undefined) {
  if (!node) return undefined;
  const tr = node.translations ?? {};
  return tr[locale.value] ?? tr[locale.value.split("-")[0]];
}

export function nodeTitle(node: NodeInfo | undefined, fallback = ""): string {
  return pick(node)?.title || node?.title || fallback;
}

export function nodeDescription(node: NodeInfo | undefined): string {
  return pick(node)?.description || node?.description || "";
}

export function portDescription(node: NodeInfo | undefined, side: "inputs" | "outputs", port: string): string {
  return pick(node)?.[side]?.[port] || node?.[side].find((p) => p.name === port)?.description || "";
}

export function fieldTitle(node: NodeInfo | undefined, field: string): string {
  return pick(node)?.config?.[field]?.title || node?.config_schema.properties?.[field]?.title || "";
}

export function fieldDescription(node: NodeInfo | undefined, field: string): string {
  return pick(node)?.config?.[field]?.description || node?.config_schema.properties?.[field]?.description || "";
}
