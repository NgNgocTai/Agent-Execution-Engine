import { useCallback, useRef } from "react";
import type { AgentEvent } from "../types";

type EventCallback = (event: AgentEvent) => void;

export function useSSE() {
  const abortRef = useRef<AbortController | null>(null);

  const stream = useCallback(
    async (response: Response, onEvent: EventCallback) => {
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      abortRef.current = new AbortController();

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop()!;

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            if (line === "data: [DONE]") return;

            try {
              const event = JSON.parse(line.slice(6)) as AgentEvent;
              onEvent(event);
            } catch {
              // skip malformed JSON
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          throw err;
        }
      }
    },
    [],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { stream, cancel };
}
