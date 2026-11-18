import { test } from "node:test";
import assert from "node:assert/strict";
import { load } from "./runtime.mjs";
const { AUTO_SKILL, runRequestBody, resolveSkill, skillLabel, statusTone } = await load("runOptions");
const options = [{ name: "auto", kind: "auto", description: "" }, { name: "alarm_statistics", kind: "builtin", description: "" }, { name: "custom_site_alarm_triage", kind: "custom", description: "" }];
test("auto is expressed by omitting skill from the request", () => {
  assert.equal("skill" in runRequestBody("hi", "openai", null, "k", AUTO_SKILL), false);
  assert.equal("skill" in runRequestBody("hi", "openai", null, "k", ""), false);
  assert.equal(runRequestBody("hi", "openai", "c1", "k", "alarm_statistics").skill, "alarm_statistics");
  assert.deepEqual(Object.keys(runRequestBody("hi", "openai", "c1", "k", "auto")).sort(), ["conversation_id", "idempotency_key", "message", "provider"]);
});
test("a selection missing from the server list falls back to auto, never to another skill", () => {
  assert.equal(resolveSkill("alarm_statistics", options), "alarm_statistics");
  assert.equal(resolveSkill("custom_revoked", options), "auto");
  assert.equal(resolveSkill("alarm_statistics", []), "auto");
});
test("labels and status tones are stable", () => {
  assert.equal(skillLabel("custom_site_alarm_triage"), "site alarm triage");
  assert.equal(skillLabel(undefined), "Auto");
  assert.equal(skillLabel("auto"), "Auto");
  assert.deepEqual(["succeeded", "failed", "unknown", "waiting_approval", "running", "queued"].map(statusTone), ["ok", "bad", "bad", "wait", "run", "run"]);
});
