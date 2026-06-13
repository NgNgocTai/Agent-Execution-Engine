import { useCallback, useRef, useState } from "react";
import type { ChatMessage, ToolCallCard } from "../types";
import { sendMessage, getConversationMessages } from "../lib/api";

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentProvider, setCurrentProvider] = useState("openai");
  const abortRef = useRef<AbortController | null>(null);

  const now = () =>
    new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });

  const send = useCallback(
    async (message: string) => {
      if (isStreaming || !message.trim()) return;

      const userMsg: ChatMessage = {
        role: "user",
        content: message,
        thinkingBlocks: [],
        toolCallCards: [],
        textContent: "",
        isStreaming: false,
        timestamp: now(),
      };

      const assistantMsg: ChatMessage = {
        role: "assistant",
        content: "",
        thinkingBlocks: [],
        toolCallCards: [],
        textContent: "",
        isStreaming: true,
        timestamp: now(),
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setIsStreaming(true);

      const abort = new AbortController();
      abortRef.current = abort;

      try {
        const resp = await sendMessage(message, currentProvider, conversationId);
        const reader = resp.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop()!;

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            if (line === "data: [DONE]") continue;

            try {
              const event = JSON.parse(line.slice(6));
              const etype = event.type;
              const edata = event.data;

              if (etype === "thinking") {
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  updated[last] = {
                    ...updated[last],
                    thinkingBlocks: [
                      ...updated[last].thinkingBlocks,
                      { content: edata.content, isExpanded: true },
                    ],
                  };
                  return updated;
                });
              }

              if (etype === "tool_call_start") {
                const rawName = edata.tool_name || "";
                const cleanName = rawName.replace(/<\|.*?\|>/g, "").trim();
                const card: ToolCallCard = {
                  callId: `tc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
                  toolName: cleanName,
                  arguments: edata.arguments || {},
                  status: "loading",
                  result: null,
                  error: null,
                  executionTimeMs: 0,
                  showInput: true,
                };
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  updated[last] = {
                    ...updated[last],
                    toolCallCards: [...updated[last].toolCallCards, card],
                  };
                  return updated;
                });
              }

              if (etype === "tool_call_result") {
                const status = edata.status === "success" ? "success" as const : "error" as const;
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const cards = [...prev[last].toolCallCards];
                  const loadingIdx = [...cards].reverse().findIndex((c) => c.status === "loading");
                  if (loadingIdx === -1) return prev;
                  const idx = cards.length - 1 - loadingIdx;
                  cards[idx] = {
                    ...cards[idx],
                    status,
                    result: edata.result || null,
                    error: edata.error || null,
                    executionTimeMs: edata.execution_time_ms || 0,
                  };
                  const updated = [...prev];
                  updated[last] = { ...updated[last], toolCallCards: cards };
                  return updated;
                });
              }

              if (etype === "text_delta") {
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  updated[last] = {
                    ...updated[last],
                    textContent: updated[last].textContent + (edata.content || ""),
                  };
                  return updated;
                });
              }

              if (etype === "final_answer") {
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  updated[last] = { ...updated[last], isStreaming: false };
                  return updated;
                });
                setIsStreaming(false);
              }

              if (etype === "conversation_id") {
                setConversationId(edata.conversation_id);
              }

              if (etype === "error") {
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  updated[last] = {
                    ...updated[last],
                    textContent: updated[last].textContent + `\n\n❌ ${edata.message}`,
                    isStreaming: false,
                  };
                  return updated;
                });
                setIsStreaming(false);
              }
            } catch {
              // skip malformed JSON
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setMessages((prev) => {
            const last = prev.length - 1;
            if (last < 0) return prev;
            const updated = [...prev];
            updated[last] = {
              ...updated[last],
              textContent: `❌ Network error: ${(err as Error).message}`,
              isStreaming: false,
            };
            return updated;
          });
        }
        setIsStreaming(false);
      }
    },
    [isStreaming, currentProvider, conversationId],
  );

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    setMessages([]);
    setConversationId(null);
    setIsStreaming(false);
  }, []);

  const toggleTab = useCallback((callId: string, showInput: boolean) => {
    setMessages((prev) =>
      prev.map((msg) => ({
        ...msg,
        toolCallCards: msg.toolCallCards.map((c) =>
          c.callId === callId ? { ...c, showInput } : c,
        ),
      })),
    );
  }, []);

  const loadConversation = useCallback(async (cid: string) => {
    abortRef.current?.abort();
    setIsStreaming(false);

    try {
      const data = await getConversationMessages(cid);
      const loaded: ChatMessage[] = [];

      for (const msg of data.messages) {
        if (msg.role === "user") {
          loaded.push({
            role: "user",
            content: msg.content || "",
            thinkingBlocks: [],
            toolCallCards: [],
            textContent: "",
            isStreaming: false,
            timestamp: "",
          });
        } else if (msg.role === "assistant" && msg.tool_calls) {
          loaded.push({
            role: "assistant",
            content: "",
            thinkingBlocks: [],
            toolCallCards: msg.tool_calls.map((tc) => ({
              callId: tc.id,
              toolName: tc.function.name,
              arguments: JSON.parse(tc.function.arguments || "{}"),
              status: "success" as const,
              result: null,
              error: null,
              executionTimeMs: 0,
              showInput: true,
            })),
            textContent: "",
            isStreaming: false,
            timestamp: "",
          });
        } else if (msg.role === "tool") {
          const lastAssistant = [...loaded].reverse().find((m) => m.role === "assistant");
          if (lastAssistant) {
            const lastCard = lastAssistant.toolCallCards[lastAssistant.toolCallCards.length - 1];
            if (lastCard) {
              try {
                lastCard.result = JSON.parse(msg.content || "{}");
              } catch {
                lastCard.result = msg.content || null;
              }
            }
          }
        } else if (msg.role === "assistant" && msg.content) {
          loaded.push({
            role: "assistant",
            content: "",
            thinkingBlocks: [],
            toolCallCards: [],
            textContent: msg.content,
            isStreaming: false,
            timestamp: "",
          });
        }
      }

      setMessages(loaded);
      setConversationId(cid);
    } catch (err) {
      console.error("Failed to load conversation:", err);
    }
  }, []);

  const switchProvider = useCallback(
    (provider: string) => {
      setCurrentProvider(provider);
      newChat();
    },
    [newChat],
  );

  return {
    messages,
    conversationId,
    isStreaming,
    currentProvider,
    send,
    newChat,
    switchProvider,
    toggleTab,
    loadConversation,
  };
}
