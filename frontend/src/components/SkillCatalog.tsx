import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
import SkillValidationReport, { type ValidationReport } from "./SkillValidationReport";
interface Version { id: string; name: string; version: string; content_hash: string; status: string; uploaded_by: string; reviewed_by: string | null; review_notes: string | null; manifest: Record<string, unknown>; validation_report?: ValidationReport; report_hash?: string }
interface Preview { manifest: Record<string, unknown>; content_hash: string; files: Array<{path: string; size: number}>; report: ValidationReport }
class SkillRequestError extends Error { report?: ValidationReport; constructor(message: string, report?: ValidationReport) { super(message); this.report = report; } }
interface Catalog { builtins: Array<{ name: string; version: string; description: string; risk: string; instructions?: string }>; versions: Version[]; active: Record<string, string> }
async function json(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new SkillRequestError(typeof data.detail === "string" ? data.detail : data.detail?.message || "Request failed", data.detail?.report);
  return data;
}
export default function SkillCatalog() {
  const [catalog, setCatalog] = useState<Catalog>({ builtins: [], versions: [], active: {} });
  const [selected, setSelected] = useState<Version | null>(null);
  const [source, setSource] = useState<Record<string, string>>({});
  const [previous, setPrevious] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [failedReport, setFailedReport] = useState<ValidationReport | null>(null);
  const [useLlm, setUseLlm] = useState(false);
  const [provider, setProvider] = useState("openai");
  const failure = (error: unknown) => { setError((error as Error).message); setFailedReport((error as SkillRequestError).report || null); };
  const refresh = async () => setCatalog(await json(await apiFetch("/skills")));
  useEffect(() => { void refresh().catch(failure => setError(failure.message)); }, []);
  const action = async (operation: string) => {
    if (!selected) return;
    setBusy(true); setError(""); setFailedReport(null);
    try {
      await json(await apiFetch(`/skills/${selected.id}/${operation === "reject" ? "review" : operation}`, { method: "POST", headers: { "Content-Type": "application/json" },
        ...(["review", "reject"].includes(operation) ? { body: JSON.stringify({ content_hash: selected.content_hash, report_hash: selected.report_hash, approved: operation === "review", notes }) } : {}) }));
      await refresh(); setSelected(null); setSource({}); setReviewed(false);
    } catch (error) { failure(error); await refresh(); setSelected(null); }
    finally { setBusy(false); }
  };
  return <main className="min-h-screen bg-slate-50 p-8 pt-20 text-slate-900">
    <h1 className="text-2xl font-bold">Skill catalog</h1>
    <p className="my-3">Built-in tools keep their existing connectors. Custom skills transform supplied data in an isolated environment without network access.</p>
    {error && <p role="alert" className="text-red-700 my-3">{error}</p>}
    {failedReport && <SkillValidationReport report={failedReport} />}
    <h2 className="font-bold mt-6">Built-in skills</h2>
    <div className="grid md:grid-cols-2 gap-3 my-3">{catalog.builtins.map(skill => <details key={skill.name} className="border rounded p-4 bg-white"><summary>{skill.name} · {skill.version} · {skill.risk}</summary><p className="my-2">{skill.description}</p>{skill.instructions && <pre className="whitespace-pre-wrap text-xs max-h-80 overflow-auto">{skill.instructions}</pre>}</details>)}</div>
    <h2 className="font-bold mt-6">Upload a version</h2>
    <p className="text-sm my-2">ZIP, up to 2 MiB: manifest.json, or SKILL.md with scripts/ and optional runtime.json. Inspect the package before uploading. Custom scripts receive supplied data only, with network access disabled.</p>
    <input aria-label="Skill ZIP" type="file" accept=".zip" disabled={busy} onChange={async event => {
      const file = event.target.files?.[0]; if (!file) return;
      setFile(null); setPreview(null); setBusy(true); setError(""); setFailedReport(null);
      try { const data = await json(await apiFetch("/skills/inspect", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file })); setPreview(data); setFile(file); }
      catch (error) { failure(error); } finally { setBusy(false); event.target.value = ""; }
    }} />
    {preview && <section aria-label="Skill package preview" className="border rounded p-3 my-3"><h3 className="font-semibold">Package preview</h3><pre className="text-xs whitespace-pre-wrap">{JSON.stringify(preview.manifest, null, 2)}</pre><ul>{preview.files.map(item => <li key={item.path}>{item.path} · {item.size} bytes</li>)}</ul><SkillValidationReport report={preview.report} /><button className="border rounded p-2" disabled={busy || !file} onClick={async () => {
      if (!file) return; setBusy(true); setError(""); setFailedReport(null);
      try { await json(await apiFetch("/skills", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file })); setFile(null); setPreview(null); await refresh(); }
      catch (error) { failure(error); } finally { setBusy(false); }
    }}>Upload inspected package</button></section>}
    <table className="w-full text-left my-4"><thead><tr><th>Name/version</th><th>Status</th><th>Uploader/reviewer</th><th>Source</th></tr></thead><tbody>{catalog.versions.map(version => <tr key={version.id} className="border-b"><td className="py-2">{version.name} {version.version}</td><td>{version.status}{catalog.active[version.name] === version.id ? " · active" : ""}</td><td>{version.uploaded_by} / {version.reviewed_by || "pending"}</td><td><button className="underline" onClick={async () => {
      if (busy) return; setBusy(true); setError(""); setReviewed(false); setNotes(""); setPrevious({}); setUseLlm(false); setFailedReport(null);
      try { const data = await json(await apiFetch(`/skills/${version.id}/source`)); setSelected(version); setSource(data.files); }
      catch (error) { failure(error); } finally { setBusy(false); }
    }}>Inspect</button></td></tr>)}</tbody></table>
    {selected && <section className="border rounded p-4 bg-white"><h2 className="font-bold">{selected.name} {selected.version}</h2><p className="font-mono text-xs break-all my-2">SHA-256 {selected.content_hash}</p>
      <label className="block my-3">Compare source with another version <select aria-label="Compare skill version" className="border rounded p-1" defaultValue="" key={selected.id} onChange={async event => {
        if (!event.target.value) { setPrevious({}); return; }
        try { const data = await json(await apiFetch(`/skills/${event.target.value}/source`)); setPrevious(data.files); }
        catch (failure) { setError((failure as Error).message); }
      }}><option value="">No comparison</option>{catalog.versions.filter(version => version.name === selected.name && version.id !== selected.id).map(version => <option key={version.id} value={version.id}>{version.version} ({version.status})</option>)}</select></label>
      <pre className="text-xs overflow-auto p-3 bg-slate-50">{JSON.stringify(selected.manifest, null, 2)}</pre>
      <SkillValidationReport report={selected.validation_report} />
      {selected.status === "quarantined" && <div className="grid gap-2 my-3"><label><input type="checkbox" checked={useLlm} disabled={busy} onChange={event => setUseLlm(event.target.checked)} /> Allow sending this skill's source to the selected LLM provider for domain and run-spec review (may incur charges).</label><select aria-label="Skill review provider" disabled={busy || !useLlm} value={provider} onChange={event => setProvider(event.target.value)}><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="deepseek">DeepSeek</option></select><button className="border rounded p-2" disabled={busy} onClick={async () => {
        setBusy(true); setError(""); setFailedReport(null); setReviewed(false);
        try { const version = await json(await apiFetch(`/skills/${selected.id}/analyze`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content_hash: selected.content_hash, use_llm: useLlm, provider }) })); setSelected(version); await refresh(); }
        catch (error) { failure(error); } finally { setBusy(false); }
      }}>Run validation{useLlm ? " with LLM" : " offline"}</button></div>}
      {[...new Set([...Object.keys(source), ...Object.keys(previous)])].sort().map(path => <details key={path} className="my-2" open><summary>{path}{Object.keys(previous).length ? source[path] === previous[path] ? " · unchanged" : " · changed" : ""}</summary><div className={Object.keys(previous).length ? "grid md:grid-cols-2 gap-3" : ""}>{Object.keys(previous).length > 0 && <pre aria-label={`Previous ${path}`} className="whitespace-pre-wrap text-xs p-3 bg-amber-50 max-h-96 overflow-auto">{previous[path] || "(file absent)"}</pre>}<pre aria-label={`Selected ${path}`} className="whitespace-pre-wrap text-xs p-3 bg-slate-50 max-h-96 overflow-auto">{source[path] || "(file absent)"}</pre></div></details>)}
      {selected.review_notes && <p>Review: {selected.review_notes}</p>}
      {selected.status === "quarantined" && <div className="grid gap-2 mt-4"><label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} /> I reviewed this version's source, inputs and permissions.</label><textarea aria-label="Review notes" className="border p-2" value={notes} onChange={event => setNotes(event.target.value)} placeholder="Review notes (at least 10 characters)" /><button className="border rounded p-2" disabled={busy || !reviewed || notes.length < 10} onClick={() => void action("review")}>Approve review and run smoke test</button><button className="border rounded p-2" disabled={busy || notes.length < 10} onClick={() => void action("reject")}>Reject review</button></div>}
      {selected.status === "approved" && <button className="border rounded p-2 m-2" disabled={busy} onClick={() => void action("activate")}>Activate this version</button>}
      {selected.status !== "revoked" && <button className="border rounded p-2 m-2" disabled={busy} onClick={() => void action("revoke")}>Revoke this version</button>}
    </section>}
  </main>;
}
