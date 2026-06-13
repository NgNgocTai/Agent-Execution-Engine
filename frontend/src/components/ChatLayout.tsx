import { useEffect, useRef } from "react";
import { Network } from "lucide-react";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ReasoningTrace from "./ReasoningTrace";
import ChatInput from "./ChatInput";
import { useChat } from "../hooks/useChat";

export default function ChatLayout() {
  const { messages, conversationId, isStreaming, currentProvider, sessions, send, newChat, switchProvider, toggleTab } =
    useChat();
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex h-screen bg-[#f9f9f9] font-sans text-[#1a1c1c] overflow-hidden">
      <Sidebar onNewChat={newChat} sessions={sessions} />
      <div className="flex-1 flex flex-col min-w-0 ml-[260px] relative">
        <Header
          currentProvider={currentProvider}
          onSwitchProvider={switchProvider}
          conversationId={conversationId}
        />

        <main className="flex-1 overflow-y-auto px-6 pt-[88px] pb-32">
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
              <div key={i}>
                {msg.role === "user" ? (
                  <div className="flex flex-col items-end">
                    <div className="bg-[#0050cb] text-white rounded-2xl rounded-tr-none px-6 py-3.5 max-w-[85%] text-[15px] shadow-sm leading-relaxed">
                      <p>{msg.content}</p>
                    </div>
                    <div className="flex items-center gap-1.5 mt-2 px-1 text-xs font-semibold text-[#727687]">
                      <span>You</span>
                      <span className="w-1 h-1 bg-[#727687] rounded-full"></span>
                      <span>{msg.timestamp}</span>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-start w-full">
                    <div className="flex items-center gap-2.5 mb-4 px-1">
                      <div className="w-7 h-7 rounded bg-[#e8effd] flex items-center justify-center text-[#0050cb]">
                        <Network size={14} />
                      </div>
                      <span className="font-bold text-sm text-[#1a1c1c]">NOC Assistant</span>
                      <span className="text-xs font-semibold text-[#727687] ml-1">
                        {msg.timestamp}
                      </span>
                    </div>
                    <ReasoningTrace message={msg} onToggleTab={toggleTab} />
                  </div>
                )}
              </div>
            ))}
            <div ref={messagesEndRef} />
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
