import { apiFetch } from "./api";
import type { ConversationMessages } from "./api";

export interface RunSummary { run_id: string; conversation_id: string; user_message_id?: number; provider: string; status: string; sequence: number; error_code: string | null }
export interface RunSnapshot extends RunSummary { messages: ConversationMessages["messages"]; pending_approvals: ConversationMessages["pending_approvals"] }

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (HTTP ${response.status})`);
  }
  return response.json();
}
export const startRun = async (message: string, provider: string, conversationId: string | null, key: string, signal?: AbortSignal) => json<RunSummary>(await apiFetch("/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, provider, conversation_id: conversationId, idempotency_key: key }), signal }));
export const getRun = async (id: string, signal?: AbortSignal) => json<RunSnapshot>(await apiFetch(`/runs/${encodeURIComponent(id)}`, { signal }));
export const getRuns = async (id: string, signal?: AbortSignal) => json<{ runs: RunSummary[] }>(await apiFetch(`/conversations/${encodeURIComponent(id)}/runs`, { signal }));
export const cancelRun = async (id: string) => json<RunSummary>(await apiFetch(`/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }));
export const decideRun = async (id: string, callId: string, approved: boolean) => json<RunSummary>(await apiFetch(`/runs/${encodeURIComponent(id)}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: callId, approved }) }));
export const streamRun = (id: string, after: number, signal: AbortSignal) => apiFetch(`/runs/${encodeURIComponent(id)}/events?after=${after}`, { signal });
export const interveneRun = async (id: string, message: string) => json<{ status: string }>(await apiFetch(`/runs/${encodeURIComponent(id)}/interventions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }) }));
export const reconcileRun = async (id: string, outcome: string, evidence: string) => json<RunSummary>(await apiFetch(`/operations/runs/${encodeURIComponent(id)}/reconcile`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ outcome, evidence }) }));
