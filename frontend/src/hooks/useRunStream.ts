import { useEffect, useRef, useState } from "react";
import { emptyRun, reduceRun, activeStatus, type RunEvent, type RunState } from "../lib/runReducer";
import { getRun, streamRun } from "../lib/runs";
import { readSSE } from "../lib/sse";
import { chatHistory } from "../lib/chatHistory";

export function useRunStream(runId: string | null, epoch: number) {
  const [state, setState] = useState<RunState | null>(null);
  const [connection, setConnection] = useState("connected");
  const cursor = useRef({ runId, sequence: 0 });
  useEffect(() => {
    if (!runId) { cursor.current = { runId: null, sequence: 0 }; setState(null); return; }
    if (cursor.current.runId !== runId) {
      cursor.current = { runId, sequence: 0 };
      setState(emptyRun(runId));
    }
    const abort = new AbortController();
    const connect = async () => {
      let attempts = 0;
      while (!abort.signal.aborted) {
        try {
          const response = await streamRun(runId, cursor.current.sequence, abort.signal);
          if (response.status === 409) {
            const snapshot = await getRun(runId, abort.signal);
            if (abort.signal.aborted) return;
            const messages = chatHistory({ conversation_id: snapshot.conversation_id, provider: snapshot.provider, messages: snapshot.messages, pending_approvals: snapshot.pending_approvals });
            const assistant = messages.filter(message => message.role === "assistant").slice(-1)[0];
            cursor.current.sequence = snapshot.sequence;
            setState({ ...emptyRun(runId), sequence: snapshot.sequence, status: snapshot.status, text: assistant?.textContent || "", cards: assistant?.toolCallCards || [] });
            setConnection("connected");
            if (!activeStatus(snapshot.status)) return;
            continue;
          }
          setConnection("connected");
          for await (const frame of readSSE(response)) {
            if (abort.signal.aborted) return;
            const event = JSON.parse(frame.data) as RunEvent;
            if (event.run_id !== runId || event.sequence <= cursor.current.sequence) continue;
            cursor.current.sequence = event.sequence;
            setState(previous => reduceRun(previous?.runId === runId ? previous : emptyRun(runId), event));
          }
          const snapshot = await getRun(runId, abort.signal);
          if (abort.signal.aborted) return;
          setState(previous => previous ? { ...previous, status: snapshot.status } : previous);
          if (!activeStatus(snapshot.status)) return;
          attempts = 0;
        } catch {
          if (abort.signal.aborted) return;
          setConnection("reconnecting");
        }
        await new Promise<void>(resolve => {
          const timer = setTimeout(resolve, Math.min(1000 * 2 ** attempts++, 15000));
          abort.signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
        });
      }
    };
    void connect();
    return () => abort.abort();
  }, [runId, epoch]);
  return { state: state?.runId === runId ? state : null, setState, connection };
}
