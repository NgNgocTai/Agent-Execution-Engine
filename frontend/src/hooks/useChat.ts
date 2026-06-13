import { useCallback, useRef, useState, useEffect } from "react";
import type { AgentEvent, ChatMessage, ToolCallCard, ThinkingBlock } from "../types";
import { sendMessage, getConversationMessages } from "../lib/api";
import { useSSE } from "./useSSE";

interface SessionEntry {
  id: string;
  title: string;
  provider: string;
  timestamp: string;
}

const SESSIONS_KEY = "noc_agent_sessions";

function loadSessions(): SessionEntry[] {
  try {
    return JSON.parse(localStorage.getItem(SESSIONS_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveSession(entry: SessionEntry) {
  const sessions = loadSessions();
  const exists = sessions.find((s) => s.id === entry.id);
  if (!exists) {
    sessions.unshift(entry);
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions.slice(0, 50)));
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentProvider, setCurrentProvider] = useState("openai");
  const [sessions, setSessions] = useState<SessionEntry[]>(loadSessions);
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
          const rawName = event.data.tool_name as string;
          const cleanName = rawName.replace(/<\|.*?\|>/g, "").trim();
          const uniqueId = `tc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
          const card: ToolCallCard = {
            callId: uniqueId,
            toolName: cleanName,
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

        case "tool_call_result": {
          const resultStatus = event.data.status === "success" ? "success" as const : "error" as const;
          updateLastAssistant((msg) => {
            const idx = [...msg.toolCallCards].reverse().findIndex((c) => c.status === "loading");
            if (idx === -1) return msg;
            const realIdx = msg.toolCallCards.length - 1 - idx;
            const updated = [...msg.toolCallCards];
            updated[realIdx] = {
              ...updated[realIdx],
              status: resultStatus,
              result: (event.data.result as Record<string, unknown>) || null,
              error: (event.data.error as string) || null,
              executionTimeMs: (event.data.execution_time_ms as number) || 0,
            };
            return { ...msg, toolCallCards: updated };
          });
          break;
        }

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

        case "conversation_id": {
          const cid = event.data.conversation_id as string;
          setConversationId(cid);
          const firstUser = messagesRef.current.find((m) => m.role === "user");
          const entry: SessionEntry = {
            id: cid,
            title: firstUser?.content.slice(0, 50) || "New chat",
            provider: currentProvider,
            timestamp: now(),
          };
          saveSession(entry);
          setSessions(loadSessions());
          break;
        }

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

  const loadConversation = useCallback(
    async (cid: string) => {
      cancel();
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
    },
    [cancel],
  );

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
    sessions,
    send,
    newChat,
    switchProvider,
    toggleTab,
    loadConversation,
  };
}
