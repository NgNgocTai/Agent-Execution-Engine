import { useState, useRef, useEffect } from "react";
import { Send, ChevronDown } from "lucide-react";

interface Props {
  onSend: (message: string) => void;
  isStreaming: boolean;
  currentProvider: string;
  onSwitchProvider: (provider: string) => void;
}

const PROVIDERS = [
  { id: "openai", label: "OpenAI", color: "#0050cb" },
  { id: "anthropic", label: "Anthropic", color: "#d97706" },
];

export default function ChatInput({ onSend, isStreaming, currentProvider, onSwitchProvider }: Props) {
  const [message, setMessage] = useState("");
  const [showProviders, setShowProviders] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const provider = PROVIDERS.find((p) => p.id === currentProvider) || PROVIDERS[0];

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "56px";
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 120) + "px";
    }
  }, [message]);

  const handleSend = () => {
    if (message.trim() && !isStreaming) {
      onSend(message.trim());
      setMessage("");
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="absolute bottom-0 w-full bg-[#f9f9f9] border-t border-[#e2e2e2] px-6 py-4 pb-6">
      <div className="max-w-[800px] mx-auto relative group">
        <textarea
          ref={textareaRef}
          className="w-full bg-white border border-[#e2e2e2] rounded-[24px] pl-6 pr-32 py-4 focus:outline-none focus:ring-2 focus:ring-[#0050cb]/20 focus:border-[#0050cb] transition-all shadow-sm resize-none overflow-hidden text-[15px] disabled:opacity-50"
          placeholder={isStreaming ? "Đang xử lý..." : "Hỏi về tình trạng mạng..."}
          rows={1}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={isStreaming}
        />
        <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-2">
          <div className="relative">
            <button
              onClick={() => setShowProviders(!showProviders)}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-[#f3f3f4] rounded-full text-[11px] font-bold text-[#424656] border border-[#e2e2e2] hover:bg-[#e8effd] transition-colors"
            >
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: provider.color }}></span>
              <span>{provider.label}</span>
              <ChevronDown size={12} />
            </button>
            {showProviders && (
              <div className="absolute bottom-full right-0 mb-2 bg-white border border-[#e2e2e2] rounded-lg shadow-lg overflow-hidden min-w-[140px]">
                {PROVIDERS.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => {
                      onSwitchProvider(p.id);
                      setShowProviders(false);
                    }}
                    className={`flex items-center gap-2 w-full px-4 py-2.5 text-sm font-bold hover:bg-[#f3f3f4] transition-colors ${
                      p.id === currentProvider ? "text-[#0050cb] bg-[#f3f7fd]" : "text-[#424656]"
                    }`}
                  >
                    <span className="w-2 h-2 rounded-full" style={{ background: p.color }}></span>
                    {p.label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            onClick={handleSend}
            disabled={!message.trim() || isStreaming}
            className="w-10 h-10 flex items-center justify-center bg-[#0050cb] text-white rounded-full transition-transform active:scale-95 shadow-md hover:bg-[#003fa4] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Send size={18} className="translate-x-[1px]" />
          </button>
        </div>
      </div>
      <p className="text-[11px] text-center mt-3 text-[#727687]">
        NOC Agent Assistant can make mistakes. Always verify alarm data.
      </p>
    </div>
  );
}
