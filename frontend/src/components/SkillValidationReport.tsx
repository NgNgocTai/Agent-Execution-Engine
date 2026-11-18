export interface ValidationReport {
  policy_version?: string;
  ready_for_review?: boolean;
  stages?: Array<{ stage: string; status: string; score?: number; domain_score?: number; reason?: string; keywords?: string[]; suspicious_points?: string[]; scripts?: unknown[]; budget?: unknown; origin?: string; script_count?: number; findings?: Array<{ path: string; line: number; code: string; message: string }>; error_code?: string }>;
  scripts?: Array<{ path: string; hash: string; status: string; purpose: string; input_schema: unknown; sample_input: unknown; output_contract: string; output_schema?: unknown; timeout_seconds: number; error_code?: string; result?: unknown }>;
  stage?: string;
  status?: string;
  findings?: Array<{ path: string; line: number; code: string; message: string }>;
}

export default function SkillValidationReport({ report }: { report: ValidationReport | null | undefined }) {
  if (!report) return <p className="my-3 text-sm">No pipeline report yet. Run validation before activation.</p>;
  const stages = report.stages || [{ stage: report.stage || "validation", status: report.status || "failed", findings: report.findings }];
  return <section aria-label="Skill validation report" className="border rounded p-4 my-4 bg-white">
    <h3 className="font-semibold">Validation pipeline</h3>
    {report.policy_version && <p className="text-xs text-slate-600 my-1">{report.policy_version}</p>}
    <ol className="space-y-3 my-3">{stages.map(stage => <li key={stage.stage}>
      <strong>{stage.stage}</strong>: <span>{stage.status}</span>
      {stage.error_code && <p className="text-red-700">{stage.error_code}</p>}
      {stage.score !== undefined && <p>Taxonomy score: {stage.score}; matched: {stage.keywords?.join(", ") || "none"}</p>}
      {stage.domain_score !== undefined && <p>Domain score: {stage.domain_score}</p>}
      {stage.reason && <p className="whitespace-pre-wrap">{stage.reason}</p>}
      {stage.script_count !== undefined && <p>{stage.script_count} script(s) · {stage.origin}</p>}
      {stage.suspicious_points?.map((point, index) => <p className="text-amber-800" key={index}>{point}</p>)}
      {stage.scripts && <details><summary>LLM run-spec suggestions and usage estimate</summary><pre className="text-xs whitespace-pre-wrap max-h-80 overflow-auto">{JSON.stringify({ suggestions: stage.scripts, budget: stage.budget }, null, 2)}</pre></details>}
      {stage.findings?.map((finding, index) => <p key={index} className="text-sm text-red-700">{finding.path}:{finding.line} · {finding.code} · {finding.message}</p>)}
    </li>)}</ol>
    {report.scripts?.map(script => <details key={script.path} className="my-2 border rounded p-2">
      <summary>{script.path} · {script.status}</summary>
      <p className="my-2">{script.purpose}</p><p>Output: {script.output_contract} · Timeout: {script.timeout_seconds}s</p>
      <p className="font-mono text-xs break-all my-2">SHA-256 {script.hash}</p>
      {script.error_code && <p className="text-red-700">{script.error_code}</p>}
      <pre className="text-xs whitespace-pre-wrap max-h-80 overflow-auto p-2 bg-slate-50">{JSON.stringify({ input_schema: script.input_schema, output_schema: script.output_schema, sample_input: script.sample_input, smoke_result: script.result }, null, 2)}</pre>
    </details>)}
    <p className="text-sm mt-3">Static analysis and domain scores support review. Every executable script must pass its isolated smoke test and a different person must approve the package.</p>
  </section>;
}
