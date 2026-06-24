import { ChevronDown } from "lucide-react";

interface Props {
  currentProvider: string;
  onSwitchProvider: (provider: string) => void;
  conversationId: string | null;
}

const PROVIDERS = [
  { id: "openai", label: "OpenAI" },
  { id: "anthropic", label: "Anthropic" },
  { id: "deepseek", label: "DeepSeek" },
];

export default function Header({ currentProvider, onSwitchProvider, conversationId }: Props) {
  return (
    <header className="flex justify-between items-center h-16 px-6 shrink-0 bg-[#f9f9f9] border-b border-[#e2e2e2]">
      <div className="flex items-center gap-4">
        <span className="text-lg font-bold text-[#1a1c1c] tracking-tight">NOC Agent Assistant</span>
        <div className="h-4 w-[1px] bg-[#e2e2e2]"></div>
        <div className="relative group">
          <select
            value={currentProvider}
            onChange={(e) => {
              const newProvider = e.target.value;
              if (newProvider !== currentProvider) {
                onSwitchProvider(newProvider);
              }
            }}
            className="appearance-none flex items-center gap-1.5 px-3 py-1.5 bg-[#eeeeee] rounded-full text-xs font-bold text-[#0050cb] hover:bg-[#e8effd] transition-colors border border-[#e2e2e2] cursor-pointer pr-7"
          >
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        {conversationId && (
          <span className="text-[10px] font-mono text-[#727687] bg-[#f3f3f4] px-2 py-1 rounded border border-[#e2e2e2]">
            {conversationId.slice(0, 8)}...
          </span>
        )}
      </div>
    </header>
  );
}
