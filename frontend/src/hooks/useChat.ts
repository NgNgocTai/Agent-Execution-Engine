import { useCallback, useRef, useState } from "react";
import type { ChatMessage, ToolCallCard } from "../types";
import { sendMessage, getConversationMessages } from "../lib/api";

function appendThinkingContent(message: ChatMessage, content: string, isExpanded: boolean) {
  const lastBlock = message.thinkingBlocks[message.thinkingBlocks.length - 1];
  if (lastBlock) {
    lastBlock.content += `${lastBlock.content ? "\n\n" : ""}${content}`;
  } else {
    message.thinkingBlocks.push({ content, isExpanded });
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentProvider, setCurrentProvider] = useState("openai");
  const abortRef = useRef<AbortController | null>(null);
  const lastThinkingIterationRef = useRef<number | null>(null);

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
      lastThinkingIterationRef.current = null;

      const abort = new AbortController();
      abortRef.current = abort;

      try {
        const resp = await sendMessage(message, currentProvider, conversationId, abort.signal);

        if (!resp.ok) {
          const text = await resp.text();
          let errorMessage = text || `HTTP ${resp.status}`;
          try {
            const parsed = JSON.parse(text);
            errorMessage = parsed.error || parsed.detail || errorMessage;
          } catch {
            // keep raw text
          }
          throw new Error(errorMessage);
        }

        if (!resp.body) {
          throw new Error("Server returned an empty response body.");
        }

        const contentType = resp.headers.get("content-type") || "";
        if (!contentType.includes("text/event-stream")) {
          const text = await resp.text();
          let errorMessage = text || "Server returned a non-streaming response.";
          try {
            const parsed = JSON.parse(text);
            errorMessage = parsed.error || parsed.detail || errorMessage;
          } catch {
            // keep raw text
          }
          throw new Error(errorMessage);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let sawTerminalEvent = false;

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
                const iteration = typeof event.iteration === "number" ? event.iteration : null;
                setMessages((prev) => {
                  const last = prev.length - 1;
                  if (last < 0 || prev[last].role !== "assistant") return prev;
                  const updated = [...prev];
                  const blocks = [...updated[last].thinkingBlocks];
                  const content =
                    iteration !== null &&
                    lastThinkingIterationRef.current !== null &&
                    iteration !== lastThinkingIterationRef.current
                      ? `\n\n${edata.content || ""}`
                      : edata.content || "";
                  if (blocks.length > 0) {
                    // Append vào thinking block cuối cùng (streaming dần)
                    blocks[blocks.length - 1] = {
                      ...blocks[blocks.length - 1],
                      content: blocks[blocks.length - 1].content + content,
                    };
                  } else {
                    // Tạo thinking block mới
                    blocks.push({ content: edata.content || "", isExpanded: true });
                  }
                  updated[last] = { ...updated[last], thinkingBlocks: blocks };
                  return updated;
                });
                lastThinkingIterationRef.current = iteration;
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
                // business_error (như không có data) vẫn coi là tool chạy thành công trên UI
                const status = (edata.status === "success" || edata.status === "business_error")
                  ? "success" as const
                  : "error" as const;

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
                sawTerminalEvent = true;
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
                sawTerminalEvent = true;
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

        if (!sawTerminalEvent) {
          setMessages((prev) => {
            const last = prev.length - 1;
            if (last < 0 || prev[last].role !== "assistant") return prev;
            const updated = [...prev];
            updated[last] = { ...updated[last], isStreaming: false };
            return updated;
          });
          setIsStreaming(false);
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
    lastThinkingIterationRef.current = null;
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
        } else {
          // Group consecutive assistant and tool messages into a single ChatMessage
          let lastMsg = loaded[loaded.length - 1];
          if (!lastMsg || lastMsg.role !== "assistant") {
            lastMsg = {
              role: "assistant",
              content: "",
              thinkingBlocks: [],
              toolCallCards: [],
              textContent: "",
              isStreaming: false,
              timestamp: "",
            };
            loaded.push(lastMsg);
          }

          if (msg.role === "assistant") {
            if (msg.thinking_content) {
              appendThinkingContent(lastMsg, msg.thinking_content, false);
            }

            if (msg.content) {
              lastMsg.textContent += (lastMsg.textContent ? "\n\n" : "") + msg.content;
            }
            if (msg.tool_calls) {
              const newCards = msg.tool_calls.map((tc: any) => {
                // Hỗ trợ cả 2 format: OpenAI gốc và NormalizedToolCall mới
                const rawName = (tc.function && tc.function.name) || tc.tool_name || "";
                const cleanName = rawName.replace(/<\|.*?\|>/g, "").trim();
                
                let parsedArgs = tc.arguments || {};
                if (tc.function && tc.function.arguments) {
                  try {
                    parsedArgs = JSON.parse(tc.function.arguments);
                  } catch {
                    parsedArgs = {};
                  }
                } else if (typeof parsedArgs === "string") {
                  try {
                    parsedArgs = JSON.parse(parsedArgs);
                  } catch {
                    parsedArgs = {};
                  }
                }

                return {
                  callId: tc.id || tc.call_id,
                  toolName: cleanName,
                  arguments: parsedArgs,
                  status: "success" as const,
                  result: null,
                  error: null,
                  executionTimeMs: 0,
                  showInput: false,
                };
              });
              lastMsg.toolCallCards.push(...newCards);

              if (!msg.content && !msg.thinking_content) {
                const toolNames = newCards.map((c: any) => c.toolName).join(", ");
                appendThinkingContent(lastMsg, `Tiến hành gọi công cụ: ${toolNames}...`, false);
              }
            }
          } else if (msg.role === "tool") {
            const card = lastMsg.toolCallCards.find((c) => c.callId === msg.tool_call_id);
            if (card) {
              try {
                const parsed = JSON.parse(msg.content || "{}");
                card.result = parsed;
              } catch {
                card.result = msg.content || null;
              }
            } else if (lastMsg.toolCallCards.length > 0) {
              // Fallback if tool_call_id is missing/mismatched: append to the last tool card
              const lastCard = lastMsg.toolCallCards[lastMsg.toolCallCards.length - 1];
              try {
                lastCard.result = JSON.parse(msg.content || "{}");
              } catch {
                lastCard.result = msg.content || null;
              }
            }
          }
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
    },
    [],
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
