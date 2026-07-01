const API_BASE = "/api";

export async function sendMessage(
  message: string,
  provider: string,
  conversationId: string | null,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      message,
      provider,
      conversation_id: conversationId,
    }),
  });
}

export async function approveAction(
  conversationId: string,
  callId: string,
  approved: boolean,
  signal?: AbortSignal,
): Promise<Response> {
  return fetch(`${API_BASE}/chat/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      conversation_id: conversationId,
      call_id: callId,
      approved,
    }),
  });
}

export async function getConfig(): Promise<{ providers: string[] }> {
  return fetch(`${API_BASE}/config`).then((r) => r.json());
}

export interface ConversationEntry {
  id: string;
  provider: string;
  title: string;
  created_at: string;
}

export async function getConversations(): Promise<ConversationEntry[]> {
  const data = await fetch(`${API_BASE}/conversations`).then((r) => r.json());
  return data.conversations || [];
}

export async function deleteConversation(conversationId: string): Promise<boolean> {
  const data = await fetch(`${API_BASE}/conversations/${conversationId}`, {
    method: "DELETE",
  }).then((r) => r.json());
  return data.success;
}

export async function renameConversation(conversationId: string, title: string): Promise<boolean> {
  const data = await fetch(`${API_BASE}/conversations/${conversationId}/title`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  }).then((r) => r.json());
  return data.success;
}

export interface ConversationMessages {
  conversation_id: string;
  provider: string;
  messages: Array<{
    role: string;
    content?: string;
    thinking_content?: string;
    tool_calls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>;
    tool_call_id?: string;
    name?: string;
  }>;
  pending_approvals?: Array<{
    call_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
    approval: Record<string, unknown>;
    expires_at: string;
  }>;
}

export async function getConversationMessages(
  conversationId: string,
): Promise<ConversationMessages> {
  return fetch(`${API_BASE}/conversations/${conversationId}/messages`).then((r) => r.json());
}
