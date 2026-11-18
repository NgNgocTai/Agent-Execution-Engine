import { useEffect, useRef, useState } from "react";
import { getApprovalInbox, type InboxApproval } from "../lib/runs";

interface Props { enabled: boolean; refreshKey: string; onOpen: (conversationId: string) => void }

const POLL_MS = 15000;

function minutesLeft(expiresAt: string, now: number): number {
  // Server times are naive UTC.
  const at = Date.parse(expiresAt.endsWith("Z") || expiresAt.includes("+") ? expiresAt : expiresAt + "Z");
  return Math.max(0, Math.round((at - now) / 60000));
}

/** Live approvals the caller may decide. Deciding still happens in the conversation. */
export default function ApprovalInbox({ enabled, refreshKey, onOpen }: Props) {
  const [items, setItems] = useState<InboxApproval[]>([]);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const generation = useRef(0);

  useEffect(() => {
    if (!enabled) { setItems([]); return; }
    const controller = new AbortController();
    const load = () => {
      const view = ++generation.current;
      getApprovalInbox(controller.signal)
        .then(result => { if (view === generation.current) { setItems(result.approvals); setError(""); setNow(Date.now()); } })
        .catch(failure => { if (view === generation.current && !controller.signal.aborted) setError((failure as Error).message); });
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [enabled, refreshKey]);

  if (!enabled || (items.length === 0 && !error)) return null;
  return <details className="shrink-0 border-b border-[#e2e2e2] bg-[#fff8e6] px-6 py-2 text-xs" open>
    <summary className="cursor-pointer font-bold text-amber-800">Approvals waiting ({items.length})</summary>
    {error && <p role="alert" className="text-red-700 my-1">{error}</p>}
    <ul aria-label="Approval inbox" className="max-w-[800px] mx-auto my-2 divide-y divide-amber-100">
      {items.map(item => <li key={item.id} className="flex items-center gap-3 py-1.5">
        <span className="font-bold">{item.approval.action ?? item.tool_name}</span>
        <span className="font-mono">{item.approval.host}{item.approval.service ? ` / ${item.approval.service}` : ""}</span>
        <span className="text-[#727687] truncate max-w-[220px]" title={item.conversation_title ?? ""}>{item.conversation_title ?? item.conversation_id.slice(0, 8)}</span>
        <span className={minutesLeft(item.expires_at, now) <= 1 ? "text-red-700" : "text-[#727687]"}>expires in {minutesLeft(item.expires_at, now)} min</span>
        {!item.actionable && <span className="text-red-700">outside your scope</span>}
        <button className="ml-auto border rounded px-2 py-0.5 bg-white hover:bg-amber-50" onClick={() => onOpen(item.conversation_id)}>Open</button>
      </li>)}
    </ul>
  </details>;
}
