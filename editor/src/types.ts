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

/** `text.transcript/1` → `transcript` */
export function typeShort(typeId: string): string {
  return typeId.replace(/\/\d+$/, "").split(".").pop() ?? typeId;
}

const CATEGORY_COLORS: Record<string, string> = {
  input: "var(--cat-io)",
  output: "var(--cat-io)",
  speech: "var(--cat-speech)",
  agent: "var(--cat-agent)",
  logic: "var(--cat-logic)",
  identity: "var(--cat-identity)",
};

export function categoryColor(category: string): string {
  return CATEGORY_COLORS[category] ?? "var(--cat-logic)";
}

export const CATEGORY_ORDER = ["input", "speech", "agent", "logic", "identity", "output"];
