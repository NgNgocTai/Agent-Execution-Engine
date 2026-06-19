const API_BASE = "/api";

export async function sendMessage(
  message: string,
  provider: string,
  conversationId: string | null,
): Promise<Response> {
  return fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      provider,
      conversation_id: conversationId,
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

export interface ConversationMessages {
  conversation_id: string;
  provider: string;
  messages: Array<{
    role: string;
    content?: string;
    tool_calls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>;
    tool_call_id?: string;
    name?: string;
  }>;
}

export async function getConversationMessages(
  conversationId: string,
): Promise<ConversationMessages> {
  return fetch(`${API_BASE}/conversations/${conversationId}/messages`).then((r) => r.json());
}
