import { test } from "node:test";
import assert from "node:assert/strict";
import { load } from "./runtime.mjs";
const { emptyRun, reduceRun } = await load("runReducer");
const event = (sequence, type, data, run_id = "one") => ({ run_id, sequence, type, data });
test("replay and stale conversation events do not duplicate text", () => {
  const update = event(1, "text_delta", { content: "hello" });
  const state = reduceRun(emptyRun("one"), update);
  assert.equal(reduceRun(state, update), state);
  assert.equal(reduceRun(state, event(2, "text_delta", { content: "wrong" }, "two")), state);
  assert.equal(state.text, "hello");
});
test("tool cards are deduplicated and errors remain errors", () => {
  let state = reduceRun(emptyRun("one"), event(1, "tool_call_start", { call_id: "call", tool_name: "test" }));
  state = reduceRun(state, event(2, "tool_call_start", { call_id: "call", tool_name: "test" }));
  state = reduceRun(state, event(3, "tool_call_result", { call_id: "call", status: "business_error", error: "Invalid input" }));
  assert.equal(state.cards.length, 1);
  assert.equal(state.cards[0].status, "error");
});
test("timeline stays bounded while sequence continues", () => {
  let state = emptyRun("one");
  for (let sequence = 1; sequence <= 300; sequence++) state = reduceRun(state, event(sequence, "progress", { message: "Working" }));
  assert.equal(state.timeline.length, 200);
  assert.equal(state.sequence, 300);
});
