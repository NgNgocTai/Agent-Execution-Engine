export interface SSEFrame { id?: string; event?: string; data: string }

export class SSEParser {
  private buffer = "";
  private data: string[] = [];
  private id: string | undefined;
  private event: string | undefined;
  private frameSize = 0;

  feed(chunk: string, end = false): SSEFrame[] {
    this.buffer += chunk;
    if (this.buffer.length > 2_000_000) throw new Error("Event frame exceeds the size limit");
    const frames: SSEFrame[] = [];
    let index = 0;
    while (index < this.buffer.length) {
      const match = /[\r\n]/.exec(this.buffer.slice(index));
      if (!match) break;
      const position = index + match.index;
      if (this.buffer[position] === "\r" && position === this.buffer.length - 1 && !end) break;
      const line = this.buffer.slice(index, position);
      index = position + (this.buffer[position] === "\r" && this.buffer[position + 1] === "\n" ? 2 : 1);
      if (line === "") {
        if (this.data.length) frames.push({ id: this.id, event: this.event, data: this.data.join("\n") });
        this.data = [];
        this.frameSize = 0;
        this.event = undefined;
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") {
        this.frameSize += value.length;
        if (this.frameSize > 2_000_000) throw new Error("Event frame exceeds the size limit");
        this.data.push(value);
      }
      else if (field === "id" && !value.includes("\0")) this.id = value;
      else if (field === "event") this.event = value;
    }
    this.buffer = this.buffer.slice(index);
    return frames;
  }
}

export async function* readSSE(response: Response) {
  if (!response.ok) throw new Error(`Stream failed (HTTP ${response.status})`);
  if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) throw new Error("Expected an event stream");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      for (const frame of parser.feed(done ? decoder.decode() : decoder.decode(value, { stream: true }), done)) {
        if (frame.data !== "[DONE]") yield frame;
      }
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
