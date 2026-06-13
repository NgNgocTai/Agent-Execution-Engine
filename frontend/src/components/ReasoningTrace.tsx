import { Network } from "lucide-react";
import type { ChatMessage } from "../types";
import ThinkingBlock from "./ThinkingBlock";
import ToolCallCard from "./ToolCallCard";
import TextResponse from "./TextResponse";

interface Props {
  message: ChatMessage;
}

export default function ReasoningTrace({ message }: Props) {
  return (
    <div className="bg-white border border-[#e2e2e2] rounded-2xl rounded-tl-none px-6 py-5 w-full shadow-sm space-y-4">
      {message.thinkingBlocks.length > 0 && (
        <ThinkingBlock blocks={message.thinkingBlocks} />
      )}

      {message.toolCallCards.map((card) => (
        <ToolCallCard key={card.callId} card={card} />
      ))}

      {(message.textContent || message.isStreaming) && (
        <TextResponse content={message.textContent} isStreaming={message.isStreaming} />
      )}

      {!message.textContent && !message.isStreaming && message.toolCallCards.length === 0 && message.thinkingBlocks.length === 0 && (
        <div className="flex items-center gap-2 text-[#727687] text-sm">
          <div className="w-4 h-4 border-2 border-[#0050cb] border-t-transparent rounded-full animate-spin"></div>
          <span>Đang suy nghĩ...</span>
        </div>
      )}
    </div>
  );
}
