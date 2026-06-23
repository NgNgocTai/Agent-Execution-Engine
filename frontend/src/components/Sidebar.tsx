import { Plus, Network, MessageSquare, Trash2, MoreHorizontal, PenLine, Check, X as XIcon } from "lucide-react";
import type { ConversationEntry } from "../lib/api";
import { useState, useRef, useEffect } from "react";

interface Props {
  onNewChat: () => void;
  onLoadConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
  onRenameConversation: (id: string, title: string) => void;
  sessions: ConversationEntry[];
  activeConversationId: string | null;
}

export default function Sidebar({ onNewChat, onLoadConversation, onDeleteConversation, onRenameConversation, sessions, activeConversationId }: Props) {
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpenId(null);
      }
    };
    if (menuOpenId) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [menuOpenId]);

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    setMenuOpenId(null);
    setDeleteConfirmId(id);
  };

  const confirmDelete = () => {
    if (deleteConfirmId) {
      onDeleteConversation(deleteConfirmId);
      setDeleteConfirmId(null);
    }
  };

  const startRename = (e: React.MouseEvent, id: string, currentTitle: string) => {
    e.stopPropagation();
    setMenuOpenId(null);
    setRenamingId(id);
    setRenameValue(currentTitle);
  };

  const submitRename = (e: React.SyntheticEvent, id: string) => {
    e.stopPropagation();
    e.preventDefault();
    if (renamingId !== id) return;
    const currentTitle = sessions.find((s) => s.id === id)?.title;
    const trimmed = renameValue.trim();
    if (trimmed && trimmed !== currentTitle) {
      onRenameConversation(id, trimmed);
    }
    setRenamingId(null);
  };

  const cancelRename = (e: React.MouseEvent) => {
    e.stopPropagation();
    setRenamingId(null);
  };

  return (
    <>
      {deleteConfirmId && (
        <div className="fixed inset-0 bg-black/40 z-[100] flex items-center justify-center">
          <div className="bg-white rounded-xl shadow-lg p-6 max-w-sm w-full mx-4">
            <h3 className="text-lg font-bold text-[#1a1c1c] mb-2">Xóa phiên chat?</h3>
            <p className="text-sm text-[#424656] mb-6">
              Bạn có chắc chắn muốn xóa phiên chat này không? Hành động này không thể hoàn tác.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setDeleteConfirmId(null)}
                className="px-4 py-2 text-sm font-semibold text-[#424656] hover:bg-[#f3f3f4] rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                className="px-4 py-2 text-sm font-semibold text-white bg-red-500 hover:bg-red-600 rounded-lg transition-colors"
              >
                OK
              </button>
            </div>
          </div>
        </div>
      )}
      <aside className="w-[260px] flex flex-col h-screen fixed left-0 top-0 bg-white border-r border-[#e2e2e2] z-50">
        <div className="p-4 flex flex-col h-full">
          <div className="flex items-center gap-3 mb-6 px-2">
            <div className="w-8 h-8 rounded shrink-0 bg-[#0050cb] flex items-center justify-center text-white">
              <Network size={20} />
            </div>
            <div>
              <h1 className="text-lg font-bold text-[#1a1c1c] leading-tight font-sans">NOC Agent</h1>
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
            <div className="space-y-1">
              {sessions.length === 0 && (
                <p className="px-3 py-2 text-xs text-[#727687]">No sessions yet</p>
              )}
              {sessions.map((s) => (
                <div
                  key={s.id}
                  className={`group relative flex items-center gap-2 w-full text-left px-3 py-2.5 rounded-lg transition-colors cursor-pointer ${
                    s.id === activeConversationId
                      ? "bg-[#e8effd] text-[#0050cb]"
                      : "text-[#424656] hover:text-[#1a1c1c] hover:bg-[#f3f3f4]"
                  }`}
                  onClick={() => {
                    if (renamingId !== s.id) onLoadConversation(s.id);
                  }}
                >
                  <MessageSquare size={14} className="shrink-0 text-[#727687]" />
                  <div className="flex flex-col min-w-0 pr-2 flex-1">
                    {renamingId === s.id ? (
                      <form onSubmit={(e) => submitRename(e, s.id)} className="flex items-center gap-1 w-full">
                        <input
                          type="text"
                          autoFocus
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onBlur={(e) => submitRename(e, s.id)}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") {
                              e.stopPropagation();
                              setRenamingId(null);
                            }
                          }}
                          onClick={(e) => e.stopPropagation()}
                          className="w-full text-sm font-bold bg-white border border-[#0050cb] rounded px-1 py-0.5 outline-none text-[#1a1c1c]"
                        />
                      </form>
                    ) : (
                      <>
                        <span className="text-sm font-bold truncate">{s.title}</span>
                      </>
                    )}
                  </div>
                  {renamingId !== s.id && (
                    <div className="relative">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setMenuOpenId(menuOpenId === s.id ? null : s.id);
                        }}
                        className={`p-1.5 rounded transition-all ${
                          menuOpenId === s.id
                            ? "opacity-100 bg-[#e2e2e5] text-[#1a1c1c]"
                            : "opacity-0 group-hover:opacity-100 text-[#727687] hover:text-[#1a1c1c] hover:bg-[#e2e2e5]"
                        }`}
                      >
                        <MoreHorizontal size={16} />
                      </button>

                      {menuOpenId === s.id && (
                        <div
                          ref={menuRef}
                          className="absolute right-0 top-8 w-32 bg-white rounded-lg shadow-lg border border-[#e2e2e2] py-1 z-50 overflow-hidden"
                        >
                          <button
                            onClick={(e) => startRename(e, s.id, s.title)}
                            className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[#424656] hover:bg-[#f3f3f4] hover:text-[#1a1c1c] transition-colors"
                          >
                            <PenLine size={14} />
                            Rename
                          </button>
                          <button
                            onClick={(e) => handleDelete(e, s.id)}
                            className="w-full flex items-center gap-2 px-3 py-2 text-sm text-red-500 hover:bg-red-50 transition-colors"
                          >
                            <Trash2 size={14} />
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
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
    </>
  );
}
