import { ChevronRight } from "lucide-react";
import type { ThinkingBlock as ThinkingBlockType } from "../types";

interface Props {
  blocks: ThinkingBlockType[];
  isStreaming?: boolean;
}

export default function ThinkingBlock({ blocks, isStreaming = false }: Props) {
  if (blocks.length === 0) return null;

  return (
    <div className="space-y-2">
      {blocks.map((block, i) => (
        <details key={i} className="group/details" open={block.isExpanded}>
          <summary className="flex items-center gap-3 text-xs font-bold text-[#424656] cursor-pointer list-none select-none hover:text-[#0050cb] transition-colors relative z-10 w-fit">
            <div className="w-7 h-7 rounded-full bg-[#f3f3f4] flex items-center justify-center border border-[#e2e2e2]">
              <ChevronRight
                size={16}
                className="group-open/details:rotate-90 transition-transform"
              />
            </div>
            <div className="flex items-center gap-2">
              <span>{isStreaming ? "🧠 Đang suy nghĩ..." : "🧠 Quá trình suy luận"}</span>
              {isStreaming && (
                <span className="flex gap-1 ml-1">
                  <span className="w-1 h-1 bg-[#0050cb] rounded-full animate-pulse"></span>
                  <span className="w-1 h-1 bg-[#0050cb] rounded-full animate-pulse delay-75"></span>
                  <span className="w-1 h-1 bg-[#0050cb] rounded-full animate-pulse delay-150"></span>
                </span>
              )}
            </div>
          </summary>
          <div className="mt-3 pl-10 text-sm text-[#424656] italic space-y-1 font-sans">
            <p>{block.content}</p>
          </div>
        </details>
      ))}
    </div>
  );
}
