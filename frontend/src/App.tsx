import ChatLayout from "./components/ChatLayout";
import { useEffect, useState } from "react";
import { apiFetch, setBearerToken } from "./lib/api";
import SkillCatalog from "./components/SkillCatalog";

export default function App() {
  const [required, setRequired] = useState(false);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [page, setPage] = useState("chat");
  useEffect(() => {
    const listener = () => setRequired(true);
    window.addEventListener("auth-required", listener);
    return () => window.removeEventListener("auth-required", listener);
  }, []);
  if (!required) return <><nav className="fixed right-5 top-4 z-50 flex gap-2 text-sm"><button className="bg-white border rounded px-3 py-1" onClick={() => setPage("chat")}>Chat</button><button className="bg-white border rounded px-3 py-1" onClick={() => setPage("skills")}>Skills</button></nav>{page === "chat" ? <ChatLayout /> : <SkillCatalog />}</>;
  return <main className="min-h-screen flex items-center justify-center bg-slate-50"><form className="p-8 bg-white border rounded-xl grid gap-4 w-96" onSubmit={async event => {
    event.preventDefault(); setBearerToken(token); setError("");
    try {
      const response = await apiFetch("/config");
      if (!response.ok) throw new Error("Authentication failed");
      setToken(""); setRequired(false);
    } catch { setBearerToken(""); setError("Authentication failed. Check your access token."); }
  }}>
    <h1 className="text-xl font-bold">Sign in to NOC Agent</h1>
    <label>Access token<input className="block border rounded p-2 w-full mt-1" type="password" autoComplete="off" required value={token} onChange={event => setToken(event.target.value)} /></label>
    <p className="text-sm text-slate-600">The token stays in this tab's memory.</p>
    {error && <p role="alert">{error}</p>}
    <button className="bg-blue-700 text-white rounded p-2" type="submit">Sign in</button>
  </form></main>;
}
