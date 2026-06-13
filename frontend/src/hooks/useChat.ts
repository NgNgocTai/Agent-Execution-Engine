import { useCallback, useRef, useState } from "react";
import type { AgentEvent, ChatMessage, ToolCallCard, ThinkingBlock } from "../types";
import { sendMessage } from "../lib/api";
import { useSSE } from "./useSSE";

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentProvider, setCurrentProvider] = useState("openai");
  const { stream, cancel } = useSSE();
  const messagesRef = useRef<ChatMessage[]>([]);
  messagesRef.current = messages;

  const now = () =>
    new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });

  const updateLastAssistant = useCallback((updater: (msg: ChatMessage) => ChatMessage) => {
    setMessages((prev) => {
      const idx = [...prev].reverse().findIndex((m) => m.role === "assistant");
      if (idx === -1) return prev;
      const realIdx = prev.length - 1 - idx;
      const updated = [...prev];
      updated[realIdx] = updater(updated[realIdx]);
      return updated;
    });
  }, []);

  const handleEvent = useCallback(
    (event: AgentEvent) => {
      switch (event.type) {
        case "thinking":
          updateLastAssistant((msg) => ({
            ...msg,
            thinkingBlocks: [
              ...msg.thinkingBlocks,
              { content: event.data.content as string, isExpanded: true },
            ],
          }));
          break;

        case "tool_call_start": {
          const card: ToolCallCard = {
            callId: event.data.call_id as string,
            toolName: event.data.tool_name as string,
            arguments: (event.data.arguments as Record<string, unknown>) || {},
            status: "loading",
            result: null,
            error: null,
            executionTimeMs: 0,
            showInput: true,
          };
          updateLastAssistant((msg) => ({
            ...msg,
            toolCallCards: [...msg.toolCallCards, card],
          }));
          break;
        }

        case "tool_call_result":
          updateLastAssistant((msg) => ({
            ...msg,
            toolCallCards: msg.toolCallCards.map((c) =>
              c.callId === event.data.call_id
                ? {
                    ...c,
                    status: event.data.status === "success" ? "success" as const : "error" as const,
                    result: (event.data.result as Record<string, unknown>) || null,
                    error: (event.data.error as string) || null,
                    executionTimeMs: (event.data.execution_time_ms as number) || 0,
                  }
                : c,
            ),
          }));
          break;

        case "text_delta":
          updateLastAssistant((msg) => ({
            ...msg,
            textContent: msg.textContent + (event.data.content as string),
            isStreaming: true,
          }));
          break;

        case "final_answer":
          updateLastAssistant((msg) => ({
            ...msg,
            isStreaming: false,
          }));
          setIsStreaming(false);
          break;

        case "conversation_id":
          setConversationId(event.data.conversation_id as string);
          break;

        case "error":
          updateLastAssistant((msg) => ({
            ...msg,
            textContent:
              msg.textContent + `\n\n❌ Error: ${event.data.message as string}`,
            isStreaming: false,
          }));
          setIsStreaming(false);
          break;
      }
    },
    [updateLastAssistant],
  );

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

      try {
        const resp = await sendMessage(message, currentProvider, conversationId);
        await stream(resp, handleEvent);
      } catch (err) {
        updateLastAssistant((msg) => ({
          ...msg,
          textContent: `❌ Network error: ${(err as Error).message}`,
          isStreaming: false,
        }));
        setIsStreaming(false);
      }
    },
    [isStreaming, currentProvider, conversationId, stream, handleEvent, updateLastAssistant],
  );

  const newChat = useCallback(() => {
    cancel();
    setMessages([]);
    setConversationId(null);
    setIsStreaming(false);
  }, [cancel]);

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
  };
}
