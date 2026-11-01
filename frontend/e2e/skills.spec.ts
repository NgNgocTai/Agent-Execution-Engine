import { test, expect, type Page } from "@playwright/test";

async function fixture(page: Page, invalid = false) {
  let uploaded = false, active = false, uploadCount = 0;
  const report = { policy_version: "skill-review-v2", ready_for_review: false,
    stages: [{ stage: "package", status: "passed" }, { stage: "static", status: "passed" }, { stage: "taxonomy", status: "passed" }, { stage: "llm", status: "not_requested" }, { stage: "sandbox", status: "pending" }, { stage: "human", status: "pending" }],
    scripts: [{ path: "scripts/summarize.py", hash: "script-hash", status: "pending", purpose: "Summarize alarm data", input_schema: { type: "object" }, sample_input: { text: "alarm" }, output_contract: "json", timeout_seconds: 10 }] };
  const version = { id: "version-one", name: "custom_alarm_summary", version: "1.0.0", content_hash: "a".repeat(64), status: "quarantined", uploaded_by: "uploader", reviewed_by: null as string | null, review_notes: null, manifest: { name: "custom_alarm_summary", package_format: "agent_skill" }, validation_report: report, report_hash: "b".repeat(64) };
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/config") return json({ providers: ["mock"] });
    if (path === "/api/conversations") return json({ conversations: [] });
    if (path === "/api/skills/inspect") return invalid
      ? json({ detail: { message: "Static skill validation failed", report: { stage: "static", status: "failed", findings: [{ path: "scripts/unsafe.py", line: 2, code: "SYS_CAPABILITY", message: "Only standard input/output streams are available from sys" }] } } }, 400)
      : json({ manifest: version.manifest, content_hash: version.content_hash, files: [{ path: "SKILL.md", size: 120 }, { path: "scripts/summarize.py", size: 180 }], report });
    if (path === "/api/skills" && route.request().method() === "POST") { uploadCount++; uploaded = true; return json(version, 201); }
    if (path === "/api/skills") return json({ builtins: [], versions: uploaded ? [version] : [], active: active ? { custom_alarm_summary: version.id } : {} });
    if (path.endsWith("/source")) return json({ files: { "SKILL.md": "Alarm summary instructions", "scripts/summarize.py": "print('{}')" } });
    if (path.endsWith("/analyze")) {
      expect(route.request().postDataJSON().use_llm).toBe(false);
      report.ready_for_review = true; report.stages[4].status = "passed"; report.scripts[0].status = "passed"; version.report_hash = "c".repeat(64);
      return json(version);
    }
    if (path.endsWith("/review")) {
      expect(route.request().postDataJSON().report_hash).toBe("c".repeat(64));
      version.status = "approved"; version.reviewed_by = "reviewer"; report.stages[5].status = "passed";
      return json(version);
    }
    if (path.endsWith("/activate")) { active = true; return json({ active: version.id }); }
    return json({ detail: "Unexpected fixture endpoint" }, 404);
  });
  return { uploadCount: () => uploadCount };
}

test("inspect upload validate review and activate an Agent Skill without LLM calls", async ({ page }) => {
  await fixture(page);
  await page.goto("/"); await page.getByRole("button", { name: "Skills", exact: true }).click();
  await page.getByLabel("Skill ZIP", { exact: true }).setInputFiles({ name: "alarm-summary.zip", mimeType: "application/zip", buffer: Buffer.from("HTTP fixture package") });
  await expect(page.getByRole("region", { name: "Skill package preview" })).toContainText("SKILL.md");
  await page.getByRole("button", { name: "Upload inspected package" }).click();
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Run validation offline", exact: true }).click();
  await expect(page.getByLabel("Skill validation report")).toContainText("sandbox: passed");
  await page.getByLabel("I reviewed this version's source, inputs and permissions.").check();
  await page.getByLabel("Review notes").fill("Reviewed alarm script, schema and sandbox evidence.");
  await page.getByRole("button", { name: "Approve review and run smoke test" }).click();
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Activate this version" }).click();
  await expect(page.getByRole("table")).toContainText("active");
});

test("static findings show the exact source location before upload", async ({ page }) => {
  const state = await fixture(page, true);
  await page.goto("/"); await page.getByRole("button", { name: "Skills", exact: true }).click();
  await page.getByLabel("Skill ZIP", { exact: true }).setInputFiles({ name: "unsafe.zip", mimeType: "application/zip", buffer: Buffer.from("HTTP fixture package") });
  await expect(page.getByLabel("Skill validation report")).toContainText("scripts/unsafe.py:2");
  await expect(page.getByRole("button", { name: "Upload inspected package" })).toHaveCount(0);
  expect(state.uploadCount()).toBe(0);
});
