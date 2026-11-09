// Visual language: payload types own the wire and pin colors, categories own node headers.

const TYPE_COLORS: Record<string, string> = {
  "audio.pcm/1": "var(--type-audio)",
  "text.transcript/1": "var(--type-transcript)",
  "text.delta/1": "var(--type-delta)",
  "text.utterance/1": "var(--type-utterance)",
  "agent.response/1": "var(--type-response)",
  "identity.principal/1": "var(--type-principal)",
  "identity.speaker/1": "var(--type-speaker)",
};

export function typeColor(typeId: string): string {
  return TYPE_COLORS[typeId] ?? "var(--type-other)";
}

const TEXT_TYPES = new Set(["text.transcript/1", "text.utterance/1", "text.delta/1", "agent.response/1"]);

/** `text.transcript/1` → `transcript`; a union of text types → `text`, of others or `*` → `any` */
export function typeShort(typeId: string): string {
  if (typeId === "*") return "any";
  if (typeId.includes("|")) return typeId.split("|").every((t) => TEXT_TYPES.has(t)) ? "text" : "any";
  return typeId.replace(/\/\d+$/, "").split(".").pop() ?? typeId;
}

const AUDIO = "audio.pcm/1";

interface Ported {
  category: string;
  inputs: { type: string }[];
  outputs: { type: string }[];
}

/**
 * What a node is for, from its ports, for the palette filter: speech nodes split into speech-to-text
 * (audio in, text out), text-to-speech (a text stream in, audio out) and audio (the rest: gain,
 * end of command, a signal); other nodes go by their category. Nodes of new plugins land in the
 * right place without the editor knowing them.
 */
export function nodeRole(node: Ported): string {
  if (node.category !== "speech") return node.category;
  const takes = (type: string) => node.inputs.some((p) => p.type === type);
  const gives = (type: string) => node.outputs.some((p) => p.type === type);
  const givesText = node.outputs.some((p) => TEXT_TYPES.has(p.type));
  if (takes(AUDIO) && givesText && !gives(AUDIO)) return "stt";
  if (gives(AUDIO) && node.inputs.some((p) => TEXT_TYPES.has(p.type))) return "tts";
  return "audio";
}

export const ROLE_ORDER = ["stt", "tts", "audio", "agent", "logic", "identity", "debug"];

const CATEGORY_COLORS: Record<string, string> = {
  input: "var(--cat-io)",
  output: "var(--cat-io)",
  speech: "var(--cat-speech)",
  agent: "var(--cat-agent)",
  logic: "var(--cat-logic)",
  debug: "var(--cat-debug)",
  identity: "var(--cat-identity)",
};

export function categoryColor(category: string): string {
  return CATEGORY_COLORS[category] ?? "var(--cat-logic)";
}

export const CATEGORY_ORDER = ["input", "speech", "agent", "logic", "identity", "output", "debug"];
