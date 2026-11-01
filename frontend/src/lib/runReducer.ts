import type { ToolCallCard, ApprovalRequest } from "../types";

export interface RunEvent { run_id: string; sequence: number; type: string; data: Record<string, unknown> }
export interface RunState { runId: string; sequence: number; status: string; text: string; cards: ToolCallCard[]; error: string | null; timeline: Array<{ sequence: number; type: string; label: string }> }
export const emptyRun = (runId: string): RunState => ({ runId, sequence: 0, status: "queued", text: "", cards: [], error: null, timeline: [] });
export const activeStatus = (status: string) => ["queued", "running"].includes(status);

export function reduceRun(state: RunState, event: RunEvent): RunState {
  if (event.run_id !== state.runId || !Number.isSafeInteger(event.sequence) || event.sequence <= state.sequence) return state;
  const data = event.data;
  const next = { ...state, sequence: event.sequence, cards: [...state.cards] };
  if (event.type === "text_delta") next.text += String(data.content || "");
  if (event.type === "run_status") { next.status = String(data.status); if (data.error_code) next.error = String(data.message || data.error_code); }
  if (event.type === "final_answer") next.status = "succeeded";
  if (event.type === "error") { next.status = "failed"; next.error = String(data.message || "Run failed"); }
  if (["tool_call_start", "tool_call_result", "approval_required"].includes(event.type)) {
    const callId = String(data.call_id);
    let index = next.cards.findIndex(card => card.callId === callId);
    if (index < 0) {
      index = next.cards.length;
      next.cards.push({ callId, toolName: String(data.tool_name), arguments: (data.arguments || {}) as Record<string, unknown>, status: "loading", result: null, error: null, executionTimeMs: 0, showInput: true });
    }
    const card = { ...next.cards[index] };
    if (event.type === "approval_required") {
      next.status = "waiting_approval";
      card.status = "approval_required";
      card.approval = data.approval as ApprovalRequest;
      card.result = data.approval as Record<string, unknown>;
    } else if (event.type === "tool_call_result") {
      card.status = data.status === "success" ? "success" : "error";
      card.result = (data.result || null) as ToolCallCard["result"];
      card.error = data.error ? String(data.error) : null;
      card.executionTimeMs = Number(data.execution_time_ms || 0);
    }
    next.cards[index] = card;
  }
  if (!["text_delta", "thinking"].includes(event.type)) {
    next.timeline = [...state.timeline, { sequence: event.sequence, type: event.type, label: String(data.message || data.status || data.tool_name || event.type) }].slice(-200);
  }
  return next;
}
