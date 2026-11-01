import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
interface Version { id: string; name: string; version: string; content_hash: string; status: string; uploaded_by: string; reviewed_by: string | null; review_notes: string | null; manifest: Record<string, unknown> }
interface Catalog { builtins: Array<{ name: string; version: string; description: string; risk: string; instructions?: string }>; versions: Version[]; active: Record<string, string> }
async function json(response: Response) {
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed");
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
  const refresh = async () => setCatalog(await json(await apiFetch("/skills")));
  useEffect(() => { void refresh().catch(failure => setError(failure.message)); }, []);
  const action = async (operation: string) => {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      await json(await apiFetch(`/skills/${selected.id}/${operation === "reject" ? "review" : operation}`, { method: "POST", headers: { "Content-Type": "application/json" },
        ...(["review", "reject"].includes(operation) ? { body: JSON.stringify({ content_hash: selected.content_hash, approved: operation === "review", notes }) } : {}) }));
      await refresh(); setSelected(null); setSource({}); setReviewed(false);
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };
  return <main className="min-h-screen bg-slate-50 p-8 pt-20 text-slate-900">
    <h1 className="text-2xl font-bold">Skill catalog</h1>
    <p className="my-3">Built-in tools keep their existing connectors. Custom skills transform supplied data in an isolated environment without network access.</p>
    {error && <p role="alert" className="text-red-700 my-3">{error}</p>}
    <h2 className="font-bold mt-6">Built-in skills</h2>
    <div className="grid md:grid-cols-2 gap-3 my-3">{catalog.builtins.map(skill => <details key={skill.name} className="border rounded p-4 bg-white"><summary>{skill.name} · {skill.version} · {skill.risk}</summary><p className="my-2">{skill.description}</p>{skill.instructions && <pre className="whitespace-pre-wrap text-xs max-h-80 overflow-auto">{skill.instructions}</pre>}</details>)}</div>
    <h2 className="font-bold mt-6">Upload a version</h2>
    <p className="text-sm my-2">ZIP, up to 2 MiB, with manifest.json and a declared Python entrypoint. A different reviewer must review the source and pass the smoke test before activation.</p>
    <input aria-label="Skill ZIP" type="file" accept=".zip" disabled={busy} onChange={async event => {
      const file = event.target.files?.[0]; if (!file) return;
      setBusy(true); setError("");
      try { await json(await apiFetch("/skills", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file })); await refresh(); }
      catch (failure) { setError((failure as Error).message); } finally { setBusy(false); event.target.value = ""; }
    }} />
    <table className="w-full text-left my-4"><thead><tr><th>Name/version</th><th>Status</th><th>Uploader/reviewer</th><th>Source</th></tr></thead><tbody>{catalog.versions.map(version => <tr key={version.id} className="border-b"><td className="py-2">{version.name} {version.version}</td><td>{version.status}{catalog.active[version.name] === version.id ? " · active" : ""}</td><td>{version.uploaded_by} / {version.reviewed_by || "pending"}</td><td><button className="underline" onClick={async () => {
      setError(""); setReviewed(false); setNotes(""); setPrevious({});
      try { const data = await json(await apiFetch(`/skills/${version.id}/source`)); setSelected(version); setSource(data.files); }
      catch (failure) { setError((failure as Error).message); }
    }}>Inspect</button></td></tr>)}</tbody></table>
    {selected && <section className="border rounded p-4 bg-white"><h2 className="font-bold">{selected.name} {selected.version}</h2><p className="font-mono text-xs break-all my-2">SHA-256 {selected.content_hash}</p>
      <label className="block my-3">Compare source with another version <select aria-label="Compare skill version" className="border rounded p-1" defaultValue="" key={selected.id} onChange={async event => {
        if (!event.target.value) { setPrevious({}); return; }
        try { const data = await json(await apiFetch(`/skills/${event.target.value}/source`)); setPrevious(data.files); }
        catch (failure) { setError((failure as Error).message); }
      }}><option value="">No comparison</option>{catalog.versions.filter(version => version.name === selected.name && version.id !== selected.id).map(version => <option key={version.id} value={version.id}>{version.version} ({version.status})</option>)}</select></label>
      <pre className="text-xs overflow-auto p-3 bg-slate-50">{JSON.stringify(selected.manifest, null, 2)}</pre>
      {[...new Set([...Object.keys(source), ...Object.keys(previous)])].sort().map(path => <details key={path} className="my-2" open><summary>{path}{Object.keys(previous).length ? source[path] === previous[path] ? " · unchanged" : " · changed" : ""}</summary><div className={Object.keys(previous).length ? "grid md:grid-cols-2 gap-3" : ""}>{Object.keys(previous).length > 0 && <pre aria-label={`Previous ${path}`} className="whitespace-pre-wrap text-xs p-3 bg-amber-50 max-h-96 overflow-auto">{previous[path] || "(file absent)"}</pre>}<pre aria-label={`Selected ${path}`} className="whitespace-pre-wrap text-xs p-3 bg-slate-50 max-h-96 overflow-auto">{source[path] || "(file absent)"}</pre></div></details>)}
      {selected.review_notes && <p>Review: {selected.review_notes}</p>}
      {selected.status === "quarantined" && <div className="grid gap-2 mt-4"><label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} /> I reviewed this version's source, inputs and permissions.</label><textarea aria-label="Review notes" className="border p-2" value={notes} onChange={event => setNotes(event.target.value)} placeholder="Review notes (at least 10 characters)" /><button className="border rounded p-2" disabled={busy || !reviewed || notes.length < 10} onClick={() => void action("review")}>Approve review and run smoke test</button><button className="border rounded p-2" disabled={busy || notes.length < 10} onClick={() => void action("reject")}>Reject review</button></div>}
      {selected.status === "approved" && <button className="border rounded p-2 m-2" disabled={busy} onClick={() => void action("activate")}>Activate this version</button>}
      {selected.status !== "revoked" && <button className="border rounded p-2 m-2" disabled={busy} onClick={() => void action("revoke")}>Revoke this version</button>}
    </section>}
  </main>;
}
