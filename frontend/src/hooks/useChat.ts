import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { approveAction, getConversationMessages } from "../lib/api";
import { cancelRun, decideRun, getRuns, startRun, interveneRun, reconcileRun, type RunSummary } from "../lib/runs";
import { activeStatus, emptyRun } from "../lib/runReducer";
import { chatHistory, chatMessage } from "../lib/chatHistory";
import { readSSE } from "../lib/sse";
import { useRunStream } from "./useRunStream";

export function useChat() {
  const [history, setHistory] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(() => sessionStorage.getItem("noc-selected-conversation"));
  const [currentProvider, setCurrentProvider] = useState("openai");
  const [run, setRun] = useState<RunSummary | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const busyRef = useRef(false);
  const retryKey = useRef<{ signature: string; key: string } | null>(null);
  const { state, setState, connection } = useRunStream(run?.run_id || null, epoch);
  const isStreaming = busy || activeStatus(state?.status || run?.status || "");

  const loadConversation = useCallback(async (id: string) => {
    const view = ++generation.current;
    setRun(null); setError(null); setNotice(""); setConversationId(id); setBusy(true);
    try {
      const [data, listing] = await Promise.all([getConversationMessages(id), getRuns(id)]);
      if (view !== generation.current) return;
      setCurrentProvider(data.provider);
      const latest = listing.runs[0];
      if (latest) {
        let lastUser = latest.user_message_id ? data.messages.findIndex(message => message.id === latest.user_message_id) : -1;
        if (lastUser < 0) {
          lastUser = data.messages.length - 1;
          while (lastUser >= 0 && data.messages[lastUser].role !== "user") lastUser--;
        }
        setHistory(chatHistory({ ...data, messages: data.messages.slice(0, lastUser + 1), pending_approvals: [] }));
        setState(emptyRun(latest.run_id)); setRun(latest); setEpoch(value => value + 1);
      } else setHistory(chatHistory(data));
    } catch (failure) { if (view === generation.current) setError((failure as Error).message); }
    finally { if (view === generation.current) setBusy(false); }
  }, [setState]);

  useEffect(() => {
    const selected = sessionStorage.getItem("noc-selected-conversation");
    if (selected) void loadConversation(selected);
  }, [loadConversation]);
  useEffect(() => {
    if (conversationId) sessionStorage.setItem("noc-selected-conversation", conversationId);
    else sessionStorage.removeItem("noc-selected-conversation");
  }, [conversationId]);

  const send = useCallback(async (text: string) => {
    if (busyRef.current || isStreaming || state?.status === "waiting_approval" || !text.trim()) return;
    const view = generation.current;
    busyRef.current = true; setBusy(true); setError(null); setNotice("");
    const signature = JSON.stringify([text, currentProvider, conversationId]);
    if (retryKey.current?.signature !== signature) retryKey.current = { signature, key: crypto.randomUUID() };
    try {
      const accepted = await startRun(text, currentProvider, conversationId, retryKey.current.key);
      if (view !== generation.current) return;
      setHistory(previous => [...previous, ...(state ? [{ ...chatMessage("assistant", state.text), toolCallCards: state.cards }] : []), chatMessage("user", text)]);
      setState(emptyRun(accepted.run_id)); setRun(accepted); setConversationId(accepted.conversation_id);
      setEpoch(value => value + 1); retryKey.current = null;
    } catch (failure) { if (view === generation.current) setError((failure as Error).message); }
    finally { busyRef.current = false; if (view === generation.current) setBusy(false); }
  }, [state, currentProvider, conversationId, isStreaming, setState]);

  const approveDecision = useCallback(async (callId: string, approved: boolean) => {
    if (!conversationId || busyRef.current) return;
    const view = generation.current;
    busyRef.current = true; setBusy(true); setError(null);
    try {
      if (run) {
        const result = await decideRun(run.run_id, callId, approved);
        if (view !== generation.current) return;
        setState(previous => previous ? { ...previous, status: result.status, cards: previous.cards.map(card => card.callId === callId ? { ...card, status: approved && result.status !== "unknown" ? "loading" : "error", error: result.status === "unknown" ? "Outcome unknown; reconciliation required" : approved ? null : "Rejected by user" } : card) } : previous);
        setEpoch(value => value + 1);
      } else {
        const response = await approveAction(conversationId, callId, approved);
        for await (const frame of readSSE(response)) JSON.parse(frame.data);
        if (view === generation.current) await loadConversation(conversationId);
      }
    } catch (failure) { if (view === generation.current) setError((failure as Error).message); }
    finally { busyRef.current = false; if (view === generation.current) setBusy(false); }
  }, [conversationId, run, loadConversation, setState]);

  const stop = useCallback(async () => {
    if (!run) return;
    const view = generation.current;
    try { const result = await cancelRun(run.run_id); if (view === generation.current) setState(previous => previous ? { ...previous, status: result.status } : previous); }
    catch (failure) { if (view === generation.current) setError((failure as Error).message); }
  }, [run, setState]);
  const intervene = useCallback(async (text: string) => {
    if (!run) return;
    const view = generation.current;
    try { await interveneRun(run.run_id, text); if (view === generation.current) setNotice("Accepted. The intervention applies after the current tool batch; it does not change the approval action."); }
    catch (failure) { if (view === generation.current) setError((failure as Error).message); }
  }, [run]);
  const reconcile = useCallback(async (outcome: string, evidence: string) => {
    if (!run) return;
    const view = generation.current;
    try { const result = await reconcileRun(run.run_id, outcome, evidence); if (view === generation.current) { setState(previous => previous ? { ...previous, status: result.status } : previous); setNotice("Reconciliation recorded. No command was rerun."); } }
    catch (failure) { if (view === generation.current) setError((failure as Error).message); }
  }, [run, setState]);
  const newChat = useCallback(() => {
    generation.current++; setHistory([]); setRun(null); setConversationId(null); setError(null); setNotice(""); setBusy(false); retryKey.current = null;
  }, []);
  const switchProvider = useCallback((provider: string) => { newChat(); setCurrentProvider(provider); }, [newChat]);
  const toggleTab = useCallback((callId: string, showInput: boolean) => {
    setState(previous => previous ? { ...previous, cards: previous.cards.map(card => card.callId === callId ? { ...card, showInput } : card) } : previous);
    setHistory(previous => previous.map(item => ({ ...item, toolCallCards: item.toolCallCards.map(card => card.callId === callId ? { ...card, showInput } : card) })));
  }, [setState]);
  const live: ChatMessage[] = state ? [{ ...chatMessage("assistant", state.text), toolCallCards: state.cards, isStreaming: activeStatus(state.status) }] : [];
  return { messages: [...history, ...live], conversationId, currentProvider, isStreaming, send, newChat, switchProvider, toggleTab, loadConversation, approveDecision, stop,
    runStatus: state?.status || null, connection, timeline: state?.timeline || [], error: error || state?.error || null, notice, intervene, reconcile };
}
