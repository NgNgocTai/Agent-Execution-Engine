import { Plus, Network, MoreHorizontal } from "lucide-react";

interface Props {
  onNewChat: () => void;
}

export default function Sidebar({ onNewChat }: Props) {
  return (
    <aside className="w-[260px] flex flex-col h-screen fixed left-0 top-0 bg-white border-r border-[#e2e2e2] z-50">
      <div className="p-4 flex flex-col h-full">
        <div className="flex items-center gap-3 mb-6 px-2">
          <div className="w-8 h-8 rounded shrink-0 bg-[#0050cb] flex items-center justify-center text-white">
            <Network size={20} />
          </div>
          <div>
            <h1 className="text-lg font-bold text-[#1a1c1c] leading-tight font-sans">NOC Agent</h1>
            <p className="text-[10px] font-bold text-[#727687] uppercase tracking-widest leading-none mt-1">
              Enterprise<br />Dashboard
            </p>
          </div>
        </div>

        <button
          onClick={onNewChat}
          className="mb-6 flex items-center gap-3 px-4 py-3 w-full bg-[#f3f7fd] border border-[#d1e0fc] rounded-lg text-[#0050cb] font-bold transition-all hover:bg-[#e8effd]"
        >
          <Plus size={20} />
          <span className="text-sm">New chat</span>
        </button>

        <nav className="flex-1 overflow-y-auto space-y-1">
          <div className="px-2 py-2 text-[10px] font-bold text-[#727687] uppercase mb-1 tracking-wider">
            Session History
          </div>
        </nav>

        <div className="mt-auto pt-4 border-t border-[#e2e2e2]">
          <div className="flex items-center gap-3 px-3 py-3">
            <div className="w-8 h-8 rounded-full bg-[#e2e2e5] flex items-center justify-center text-[#1a1c1c] text-xs font-bold shrink-0">
              NOC
            </div>
            <div className="text-left min-w-0">
              <p className="text-sm font-bold leading-tight text-[#1a1c1c] truncate">NOC Engineer</p>
              <p className="text-[10px] text-[#727687] mt-0.5">System Admin</p>
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
