import { test } from "node:test";
import assert from "node:assert/strict";
import { load } from "./runtime.mjs";
const { SSEParser, readSSE } = await load("sse");

test("all split positions preserve CRLF and multiline data", () => {
  const input = ': heartbeat\r\nid: 42\r\nevent: update\r\ndata: {"a":\r\ndata: 1}\r\n\r\n';
  for (let split = 0; split <= input.length; split++) {
    const parser = new SSEParser();
    const frames = [...parser.feed(input.slice(0, split)), ...parser.feed(input.slice(split), true)];
    assert.deepEqual(frames, [{ id: "42", event: "update", data: '{"a":\n1}' }]);
  }
});
test("UTF-8 bytes split into individual chunks decode correctly", async () => {
  const bytes = new TextEncoder().encode('data: {"text":"kiểm tra"}\n\n');
  const stream = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } });
  const result = [];
  for await (const frame of readSSE(new Response(stream, { headers: { "Content-Type": "text/event-stream" } }))) result.push(JSON.parse(frame.data));
  assert.deepEqual(result, [{ text: "kiểm tra" }]);
});
test("unbounded partial frame is rejected", () => {
  assert.throws(() => new SSEParser().feed("x".repeat(2_000_001)), /size limit/);
});
