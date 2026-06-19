import { useEffect, useRef, useState } from "react";
import { Network } from "lucide-react";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ReasoningTrace from "./ReasoningTrace";
import ChatInput from "./ChatInput";
import { useChat } from "../hooks/useChat";
import { getConversations, type ConversationEntry } from "../lib/api";

export default function ChatLayout() {
  const {
    messages, conversationId, isStreaming, currentProvider,
    send, newChat, switchProvider, toggleTab, loadConversation,
  } = useChat();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const prevContentLen = useRef(0);
  const [conversations, setConversations] = useState<ConversationEntry[]>([]);

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
      const { deleteConversation } = await import("../lib/api");
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

  useEffect(() => {
    refreshConversations();
  }, []);

  useEffect(() => {
    const totalLen = messages.reduce(
      (sum, m) => sum + m.textContent.length + m.content.length + m.toolCallCards.length * 10,
      0,
    );
    if (totalLen > prevContentLen.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
    prevContentLen.current = totalLen;
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
        sessions={conversations}
        activeConversationId={conversationId}
      />
      <div className="flex-1 flex flex-col min-w-0 ml-[260px] h-screen">
        <Header
          currentProvider={currentProvider}
          onSwitchProvider={switchProvider}
          conversationId={conversationId}
        />

        <main ref={scrollContainerRef} className="flex-1 overflow-y-auto px-6 py-6">
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
                    <ReasoningTrace message={msg} messageIndex={i} onToggleTab={toggleTab} />
                  </div>
                )}
              </div>
            ))}
            <div ref={messagesEndRef} className="h-4" />
          </div>
        </main>

        <ChatInput
          onSend={send}
          isStreaming={isStreaming}
          currentProvider={currentProvider}
          onSwitchProvider={switchProvider}
        />
      </div>
    </div>
  );
}
