import { useEffect, useRef, useState } from "react";
import { Network } from "lucide-react";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ReasoningTrace from "./ReasoningTrace";
import ChatInput from "./ChatInput";
import RunStatus from "./RunStatus";
import { useChat } from "../hooks/useChat";
import {
  deleteConversation,
  getConfig,
  getConversations,
  renameConversation,
  type ConversationEntry,
} from "../lib/api";
import type { ProviderOption } from "../types";

const PROVIDER_META: Record<string, ProviderOption> = {
  openai: { id: "openai", label: "OpenAI", color: "#0050cb" },
  anthropic: { id: "anthropic", label: "Anthropic", color: "#d97706" },
  deepseek: { id: "deepseek", label: "DeepSeek", color: "#4f46e5" },
  mock: { id: "mock", label: "Mock", color: "#64748b" },
};

const DEFAULT_PROVIDERS = ["openai", "anthropic", "deepseek"].map(
  (id) => PROVIDER_META[id],
);

function toProviderOption(id: string): ProviderOption {
  return PROVIDER_META[id] || {
    id,
    label: id
      .split(/[-_]/)
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" "),
    color: "#64748b",
  };
}

export default function ChatLayout() {
  const {
    messages, conversationId, isStreaming, currentProvider,
    send, newChat, switchProvider, toggleTab, loadConversation, approveDecision,
    stop, runStatus, connection, timeline, error, notice, intervene, reconcile,
  } = useChat();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const prevContentLen = useRef(0);
  const prevMessageCount = useRef(0);
  const isAutoScrollEnabled = useRef(true);
  const [conversations, setConversations] = useState<ConversationEntry[]>([]);
  const [providers, setProviders] = useState<ProviderOption[]>(DEFAULT_PROVIDERS);
  const [isAdmin, setIsAdmin] = useState(false);

  const handleScroll = () => {
    if (!scrollContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollContainerRef.current;
    isAutoScrollEnabled.current = scrollHeight - scrollTop - clientHeight < 150;
  };

  const refreshConversations = async () => {
    try {
      const data = await getConversations();
      setConversations(data);
    } catch {
      // ignore
    }
  };

  const handleDeleteConversation = async (id: string) => {
    try {
      const success = await deleteConversation(id);
      if (success) {
        if (id === conversationId) {
          newChat();
        }
        refreshConversations();
      }
    } catch (e) {
      console.error("Failed to delete conversation", e);
    }
  };

  const handleRenameConversation = async (id: string, title: string) => {
    try {
      const success = await renameConversation(id, title);
      if (success) {
        refreshConversations();
      }
    } catch (e) {
      console.error("Failed to rename conversation", e);
    }
  };

  useEffect(() => {
    refreshConversations();
  }, []);

  useEffect(() => {
    let cancelled = false;

    getConfig()
      .then((config) => {
        if (cancelled || !config.providers.length) return;
        const nextProviders = config.providers.map(toProviderOption);
        setIsAdmin(config.actor?.roles.includes("admin") || false);
        setProviders(nextProviders);
        if (!conversationId && !isStreaming && !config.providers.includes(currentProvider)) {
          switchProvider(config.providers[0]);
        }
      })
      .catch(() => {
        // keep defaults
      });

    return () => {
      cancelled = true;
    };
  }, [currentProvider, switchProvider, conversationId, isStreaming]);

  useEffect(() => {
    const totalLen = messages.reduce(
      (sum, m) => sum + m.textContent.length + m.content.length + m.toolCallCards.length * 10,
      0,
    );
    const isNewMessage = messages.length > prevMessageCount.current;
    if (totalLen > prevContentLen.current) {
      if (isNewMessage || isAutoScrollEnabled.current) {
        messagesEndRef.current?.scrollIntoView({ behavior: isNewMessage ? "smooth" : "auto" });
      }
    }
    prevContentLen.current = totalLen;
    prevMessageCount.current = messages.length;
  }, [messages]);

  useEffect(() => {
    if (conversationId) {
      refreshConversations();
    }
  }, [conversationId]);

  return (
    <div className="flex h-screen bg-[#f9f9f9] font-sans text-[#1a1c1c] overflow-hidden">
      <Sidebar
        onNewChat={newChat}
        onLoadConversation={loadConversation}
        onDeleteConversation={handleDeleteConversation}
        onRenameConversation={handleRenameConversation}
        sessions={conversations}
        activeConversationId={conversationId}
      />
      <div className="flex-1 flex flex-col min-w-0 ml-[260px] h-screen">
        <Header
          currentProvider={currentProvider}
          onSwitchProvider={switchProvider}
          conversationId={conversationId}
          providers={providers}
        />

        <main ref={scrollContainerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-6 py-6">
          <div className="max-w-[800px] mx-auto space-y-8">
            {messages.length === 0 && (
              <div className="flex flex-col items-center justify-center h-[60vh] text-center">
                <div className="w-16 h-16 rounded-2xl bg-[#e8effd] flex items-center justify-center text-[#0050cb] mb-4">
                  <Network size={32} />
                </div>
                <h2 className="text-xl font-bold text-[#1a1c1c] mb-2">NOC Agent Assistant</h2>
                <p className="text-sm text-[#727687] max-w-[400px]">
                  Hỏi về tình trạng mạng, cảnh báo, hoặc sức khỏe thiết bị. Agent sẽ tự động gọi
                  công cụ phù hợp để trả lời.
                </p>
              </div>
            )}

            {messages.map((msg, i) => (
              <div key={`msg-${i}`}>
                {msg.role === "user" ? (
                  <div className="flex flex-col items-end">
                    <div className="bg-[#0050cb] text-white rounded-2xl rounded-tr-none px-6 py-3.5 max-w-[85%] text-[15px] shadow-sm leading-relaxed">
                      <p>{msg.content}</p>
                    </div>
                    {msg.timestamp && (
                      <div className="flex items-center gap-1.5 mt-2 px-1 text-xs font-semibold text-[#727687]">
                        <span>You</span>
                        <span className="w-1 h-1 bg-[#727687] rounded-full"></span>
                        <span>{msg.timestamp}</span>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col items-start w-full">
                    <div className="flex items-center gap-2.5 mb-4 px-1">
                      <div className="w-7 h-7 rounded bg-[#e8effd] flex items-center justify-center text-[#0050cb]">
                        <Network size={14} />
                      </div>
                      <span className="font-bold text-sm text-[#1a1c1c]">NOC Assistant</span>
                      {msg.timestamp && (
                        <span className="text-xs font-semibold text-[#727687] ml-1">
                          {msg.timestamp}
                        </span>
                      )}
                    </div>
                    <ReasoningTrace
                      message={msg}
                      messageIndex={i}
                      onToggleTab={toggleTab}
                      onApprovalDecision={approveDecision}
                    />
                  </div>
                )}
              </div>
            ))}
            <div ref={messagesEndRef} className="h-4" />
          </div>
        </main>

        <RunStatus status={runStatus} connection={connection} error={error} timeline={timeline} onStop={stop} notice={notice} isAdmin={isAdmin} onIntervene={intervene} onReconcile={reconcile} />
        <ChatInput
          onSend={send}
          isStreaming={isStreaming}
          currentProvider={currentProvider}
          onSwitchProvider={switchProvider}
          providers={providers}
        />
      </div>
    </div>
  );
}
