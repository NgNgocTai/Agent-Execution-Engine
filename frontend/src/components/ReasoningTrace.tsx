import type { ChatMessage } from "../types";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";
import TextResponse from "./TextResponse";

interface Props {
  message: ChatMessage;
  messageIndex: number;
  onToggleTab?: (callId: string, showInput: boolean) => void;
}

export default function ReasoningTrace({ message, messageIndex, onToggleTab }: Props) {
  const hasTrace =
    message.thinkingBlocks.length > 0 ||
    message.toolCallCards.length > 0 ||
    message.textContent.length > 0;

  return (
    <div className="bg-white border border-[#e2e2e2] rounded-2xl rounded-tl-none px-6 py-5 w-full shadow-sm">
      <div className="relative">
        {hasTrace && (
          <div className="absolute left-[13px] top-0 bottom-0 w-0.5 bg-[#e2e2e2] z-0" />
        )}

        <div className="space-y-4 relative z-10">
          {message.thinkingBlocks.map((block, i) => (
            <div key={`think-${messageIndex}-${i}`} className="relative group">
              <div className="absolute left-0 w-7 h-7 flex items-center justify-center z-10">
                <div className="w-2 h-2 rounded-full bg-[#8b5cf6] animate-pulse" />
              </div>
              <div className="ml-10">
                <ThinkingBlock blocks={[block]} />
              </div>
            </div>
          ))}

          {message.toolCallCards.map((card, i) => (
            <div key={`${messageIndex}-${card.callId}-${i}`} className="relative group">
              <div className="absolute left-0 w-7 h-7 flex items-center justify-center z-10">
                <div
                  className={`w-2 h-2 rounded-full ${
                    card.status === "loading"
                      ? "bg-[#0050cb] animate-pulse"
                      : card.status === "success"
                        ? "bg-green-500"
                        : "bg-red-500"
                  }`}
                />
              </div>
              <div className="ml-10">
                <ToolCallCard card={card} onToggleTab={onToggleTab} />
              </div>
            </div>
          ))}

          {(message.textContent || message.isStreaming) && (
            <div className="relative group">
              <div className="absolute left-0 w-7 h-7 flex items-center justify-center z-10">
                <div
                  className={`w-2 h-2 rounded-full ${
                    message.isStreaming ? "bg-[#0050cb] animate-pulse" : "bg-[#424656]"
                  }`}
                />
              </div>
              <div className="ml-10">
                <TextResponse content={message.textContent} isStreaming={message.isStreaming} />
              </div>
            </div>
          )}

          {!hasTrace && (
            <div className="relative group">
              <div className="absolute left-0 w-7 h-7 flex items-center justify-center z-10">
                <div className="w-2 h-2 rounded-full bg-[#0050cb] animate-pulse" />
              </div>
              <div className="ml-10 flex items-center gap-2 text-[#727687] text-sm">
                <div className="w-4 h-4 border-2 border-[#0050cb] border-t-transparent rounded-full animate-spin"></div>
                <span>Đang suy nghĩ...</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
