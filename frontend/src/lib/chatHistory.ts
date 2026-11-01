import type { ConversationMessages } from "./api";
import type { ChatMessage, ToolCallCard } from "../types";
export const chatMessage = (role: "user" | "assistant", text: string): ChatMessage => ({ role, content: role === "user" ? text : "", textContent: role === "assistant" ? text : "", thinkingBlocks: [], toolCallCards: [], isStreaming: false, timestamp: "" });
export function chatHistory(data: ConversationMessages): ChatMessage[] {
  const result: ChatMessage[] = [];
  for (const row of data.messages) {
    if (row.role === "user") { result.push(chatMessage("user", row.content || "")); continue; }
    let item = result[result.length - 1];
    if (!item || item.role !== "assistant") { item = chatMessage("assistant", ""); result.push(item); }
    if (row.role === "assistant") {
      item.textContent += (item.textContent && row.content ? "\n\n" : "") + (row.content || "");
      for (const raw of row.tool_calls || []) {
        const call = raw as any;
        let args = call.arguments || {};
        try { if (call.function) args = JSON.parse(call.function.arguments); } catch { args = {}; }
        item.toolCallCards.push({ callId: call.call_id || call.id, toolName: call.tool_name || call.function?.name || "tool", arguments: args, status: "loading", result: null, error: null, executionTimeMs: 0, showInput: false });
      }
    } else if (row.role === "tool") {
      const card = item.toolCallCards.find(card => card.callId === row.tool_call_id);
      if (card) {
        card.status = (row as any).is_error ? "error" : "success";
        try { card.result = JSON.parse(row.content || "null") as ToolCallCard["result"]; } catch { card.result = row.content || null; }
      }
    }
  }
  for (const pending of data.pending_approvals || []) {
    const card = result.flatMap(item => item.toolCallCards).find(item => item.callId === pending.call_id);
    if (card) { card.status = "approval_required"; card.approval = pending.approval; card.result = pending.approval; }
  }
  return result;
}
