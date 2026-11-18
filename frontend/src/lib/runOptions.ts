// Pure helpers (no imports) so they can be unit tested without a browser.
export interface SkillOption { name: string; kind: "auto" | "builtin" | "custom"; description: string; version?: string }
export interface ProviderStatus { id: string; model: string; available: boolean; reason: string | null }
export const AUTO_SKILL = "auto";

/** Body for POST /runs. "auto" is expressed by omitting skill, never by sending it. */
export function runRequestBody(message: string, provider: string, conversationId: string | null, key: string, skill: string) {
  return { message, provider, conversation_id: conversationId, idempotency_key: key, ...(skill && skill !== AUTO_SKILL ? { skill } : {}) };
}

/** A selection that no longer exists on the server falls back to auto, never to another skill. */
export function resolveSkill(selected: string, options: SkillOption[]): string {
  return options.some(option => option.name === selected) ? selected : AUTO_SKILL;
}

export function skillLabel(name: string | undefined | null): string {
  return !name || name === AUTO_SKILL ? "Auto" : name.replace(/^custom_/, "").replace(/_/g, " ");
}

export const statusTone = (status: string): "ok" | "bad" | "wait" | "run" =>
  status === "succeeded" ? "ok" : ["failed", "cancelled", "timed_out", "unknown"].includes(status) ? "bad" : status === "waiting_approval" ? "wait" : "run";
