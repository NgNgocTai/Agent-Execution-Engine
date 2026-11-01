import { useState } from "react";
interface Props { status: string | null; connection: string; error: string | null; notice: string; isAdmin: boolean; onStop: () => void; onIntervene: (text: string) => void; onReconcile: (outcome: string, evidence: string) => void; timeline: Array<{ sequence: number; type: string; label: string }> }
export default function RunStatus({ status, connection, error, notice, isAdmin, onStop, onIntervene, onReconcile, timeline }: Props) {
  const [intervention, setIntervention] = useState("");
  const [evidence, setEvidence] = useState("");
  const [outcome, setOutcome] = useState("verified_failed");
  if (!status && !error) return null;
  return <section className="px-6 py-2 border-b border-[#e2e2e2] text-sm" aria-label="Run status">
    <div className="flex items-center justify-between gap-4">
      <span role="status">{status}{connection !== "connected" ? ` · ${connection}` : ""}{status === "unknown" ? " · Requires reconciliation before another action" : ""}</span>
      {["queued", "running", "waiting_approval"].includes(status || "") && <button className="border rounded px-3 py-1" onClick={onStop}>Stop run</button>}
    </div>
    {error && <p role="alert" className="text-red-700">{error}</p>}
    {notice && <p role="status" className="text-blue-800">{notice}</p>}
    {status === "waiting_approval" && <details><summary>Add an intervention</summary><p className="text-xs my-2">Applies after this tool batch. To change the pending action, reject or cancel it.</p><textarea aria-label="Operator intervention" className="border rounded p-2 w-full" maxLength={2000} value={intervention} onChange={event => setIntervention(event.target.value)} /><button className="border rounded px-3 py-1" disabled={!intervention.trim()} onClick={() => { onIntervene(intervention); setIntervention(""); }}>Queue intervention</button></details>}
    {status === "unknown" && isAdmin && <details><summary>Record manual reconciliation</summary><p className="text-xs my-2">Verify the remote state first. This records the outcome and releases the run; it does not rerun a command.</p><select aria-label="Reconciliation outcome" value={outcome} onChange={event => setOutcome(event.target.value)}><option value="verified_succeeded">Verified succeeded</option><option value="verified_failed">Verified failed</option><option value="aborted_before_dispatch">Verified not dispatched</option></select><textarea aria-label="Reconciliation evidence" className="border rounded p-2 w-full" maxLength={4000} value={evidence} onChange={event => setEvidence(event.target.value)} placeholder="Evidence from the remote state verification (at least 20 characters)" /><button className="border rounded px-3 py-1" disabled={evidence.trim().length < 20} onClick={() => onReconcile(outcome, evidence)}>Record verified outcome</button></details>}
    {timeline.length > 0 && <details className="mt-1"><summary>Run timeline ({timeline.length})</summary><ol className="max-h-40 overflow-y-auto">{timeline.map(item => <li key={item.sequence}>#{item.sequence} {item.label}</li>)}</ol></details>}
  </section>;
}
