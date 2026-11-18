import { useEffect, useRef, useState } from "react";
import { getRun, getRuns, type RunSnapshot, type RunSummary } from "../lib/runs";
import { skillLabel, statusTone } from "../lib/runOptions";

interface Props { conversationId: string | null; refreshKey: string | null }

const TONE: Record<string, string> = { ok: "text-green-700", bad: "text-red-700", wait: "text-amber-700", run: "text-[#0050cb]" };

function modelOf(run: RunSummary): string {
  const configuration = run.configuration as { candidates?: Array<{ model?: string }>; requires_new_run?: boolean } | null | undefined;
  if (configuration?.requires_new_run) return "no pinned model (legacy run)";
  return configuration?.candidates?.[0]?.model ?? run.provider;
}

/** Past runs of the open conversation, with redacted server-side detail on demand. */
export default function RunHistory({ conversationId, refreshKey }: Props) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [open, setOpen] = useState<RunSnapshot | null>(null);
  const [error, setError] = useState("");
  const generation = useRef(0);

  useEffect(() => {
    const view = ++generation.current;
    setOpen(null); setError("");
    if (!conversationId) { setRuns([]); return; }
    const controller = new AbortController();
    getRuns(conversationId, controller.signal)
      .then(result => { if (view === generation.current) setRuns(result.runs); })
      .catch(failure => { if (view === generation.current && !controller.signal.aborted) setError((failure as Error).message); });
    return () => controller.abort();
  }, [conversationId, refreshKey]);

  const select = async (runId: string) => {
    const view = ++generation.current;
    setError("");
    try {
      const snapshot = await getRun(runId);
      if (view === generation.current) setOpen(snapshot);
    } catch (failure) { if (view === generation.current) setError((failure as Error).message); }
  };

  if (!conversationId || runs.length === 0) return null;
  return <details className="shrink-0 border-t border-[#e2e2e2] bg-white px-6 py-2 text-xs">
    <summary className="cursor-pointer font-bold text-[#424656]">Run history ({runs.length})</summary>
    {error && <p role="alert" className="text-red-700 my-1">{error}</p>}
    <ul className="max-w-[800px] mx-auto my-2 divide-y divide-[#eeeeee]">
      {runs.map(run => <li key={run.run_id}>
        <button className="flex w-full items-center gap-3 py-1.5 text-left hover:bg-[#f3f3f4]" onClick={() => select(run.run_id)} aria-label={`Open run ${run.run_id.slice(0, 8)}`}>
          <span className="font-mono text-[#727687]">{run.run_id.slice(0, 8)}</span>
          <span className={`font-bold ${TONE[statusTone(run.status)]}`}>{run.status}</span>
          <span>{skillLabel(run.skill)}</span>
          <span className="text-[#727687]">{modelOf(run)}</span>
          {run.error_code && <span className="text-red-700">{run.error_code}</span>}
          {run.created_at && <span className="ml-auto text-[#727687]">{new Date(run.created_at).toLocaleString()}</span>}
        </button>
        {open?.run_id === run.run_id && <div className="pl-4 pb-2" data-testid="run-detail">
          <p>Status {open.status} · {open.steps?.length ?? 0} step(s) · {open.pending_approvals?.length ?? 0} pending approval(s)</p>
          <ol className="list-decimal pl-5">{(open.steps ?? []).map(step => <li key={step.step_id}>
            {step.kind} <span className={TONE[statusTone(step.status)]}>{step.status}</span>
            {step.usage && <span className="text-[#727687]"> · {JSON.stringify(step.usage)}</span>}
          </li>)}</ol>
          {(open.interventions ?? []).length > 0 && <ul className="mt-1" aria-label="Interventions">{(open.interventions ?? []).map(item => <li key={item.id}>Intervention <strong>{item.status}</strong>{item.status === "undelivered" ? " (the run ended before it was applied)" : item.status === "pending" ? " (waiting for the next safe checkpoint)" : ""}: {item.message}</li>)}</ul>}
          <p className="text-[#727687] mt-1">Inputs and outputs are redacted by the server.</p>
        </div>}
      </li>)}
    </ul>
  </details>;
}
