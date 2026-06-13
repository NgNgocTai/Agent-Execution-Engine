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
