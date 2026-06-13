import { Wrench, ChevronDown, Loader2, CheckCircle2, XCircle } from "lucide-react";
import type { ToolCallCard as ToolCallCardType } from "../types";

interface Props {
  card: ToolCallCardType;
  onToggleTab?: (callId: string, showInput: boolean) => void;
}

export default function ToolCallCard({ card, onToggleTab }: Props) {
  const statusConfig = {
    loading: {
      icon: <Loader2 size={12} className="animate-spin" />,
      label: "Đang thực thi...",
      bg: "bg-[#0050cb]/5",
      text: "text-[#0050cb]",
      border: "border-[#0050cb]/20",
      cardBorder: "border-[#0050cb]/30",
    },
    success: {
      icon: <CheckCircle2 size={12} />,
      label: "SUCCESS",
      bg: "bg-green-100/60",
      text: "text-green-700",
      border: "border-green-200",
      cardBorder: "border-green-200",
    },
    error: {
      icon: <XCircle size={12} />,
      label: "ERROR",
      bg: "bg-red-100/60",
      text: "text-red-700",
      border: "border-red-200",
      cardBorder: "border-red-200",
    },
  };

  const status = statusConfig[card.status];

  return (
    <div className={`border ${status.cardBorder} rounded-xl overflow-hidden bg-[#fafafa] relative z-10`}>
      <div className="flex items-center justify-between px-4 py-3 bg-[#f3f3f4]">
        <div className="flex items-center gap-3">
          <Wrench size={16} className="text-[#727687]" />
          <span className="text-sm font-bold text-[#1a1c1c]">{card.toolName}</span>
          <div
            className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full border ${status.border} ${status.bg} ${status.text} text-[10px] font-bold uppercase`}
          >
            {status.icon}
            <span>{status.label}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {card.executionTimeMs > 0 && (
            <span className="text-[11px] text-[#727687] font-mono">
              {(card.executionTimeMs / 1000).toFixed(1)}s
            </span>
          )}
        </div>
      </div>

      <div className="p-4 bg-[#0a0a0a] font-mono text-[13px] leading-relaxed">
        <div className="flex gap-4 border-b border-white/10 mb-3 pb-1">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggleTab?.(card.callId, true);
            }}
            className={`pb-1 font-bold border-b-2 transition-colors ${
              card.showInput
                ? "text-[#0050cb] border-[#0050cb]"
                : "text-white/40 border-transparent hover:text-white/70"
            }`}
          >
            Input
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggleTab?.(card.callId, false);
            }}
            className={`pb-1 font-bold border-b-2 transition-colors ${
              !card.showInput
                ? "text-[#0050cb] border-[#0050cb]"
                : "text-white/40 border-transparent hover:text-white/70"
            }`}
          >
            Output
          </button>
        </div>
        <pre className="overflow-x-auto text-[#e2e2e5] max-h-[300px] overflow-y-auto">
          <code>
            {card.showInput
              ? JSON.stringify(card.arguments, null, 2)
              : card.result
                ? JSON.stringify(card.result, null, 2)
                : card.error
                  ? `Error: ${card.error}`
                  : "Waiting..."}
          </code>
        </pre>
      </div>
    </div>
  );
}
