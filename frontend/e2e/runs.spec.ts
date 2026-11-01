import { test, expect, type Page } from "@playwright/test";

async function fixture(page: Page, approval = false, snapshotOnly = false) {
  let started = false, approved = false, cancelled = false, reconnect = false, approvals = 0;
  const run = () => ({ run_id: "run-one", conversation_id: "conversation-one", provider: "mock", status: cancelled ? "cancelled" : approved || snapshotOnly ? "succeeded" : approval ? "waiting_approval" : "running", sequence: approved ? 7 : 4, error_code: null });
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/config") return json({ providers: ["mock"] });
    if (path === "/api/conversations") return json({ conversations: started ? [{ id: "conversation-one", title: "Fixture request", provider: "mock" }] : [] });
    if (path.endsWith("/messages")) return json({ conversation_id: "conversation-one", provider: "mock", messages: [{ role: "user", content: "Fixture request" }], pending_approvals: [] });
    if (path.endsWith("/runs") && path.includes("/conversations/")) return json({ runs: started ? [run()] : [] });
    if (path === "/api/runs" && route.request().method() === "POST") { started = true; return json(run(), 202); }
    if (path.endsWith("/approve")) { approvals++; approved = true; return json({ ...run(), status: "queued" }); }
    if (path.endsWith("/cancel")) { cancelled = true; return json(run()); }
    if (path.endsWith("/events")) {
      if (snapshotOnly) return json({ detail: { code: "SNAPSHOT_REQUIRED" } }, 409);
      if (reconnect) { reconnect = false; return route.abort("connectionreset"); }
      const after = Number(url.searchParams.get("after") || 0);
      const events: Array<{ sequence: number; type: string; data: Record<string, unknown> }> = [
        { sequence: 1, type: "run_status", data: { status: "running" } },
        { sequence: 2, type: "tool_call_start", data: { call_id: "call-one", tool_name: "node_health_remediate", arguments: { host: "fixture" } } },
        { sequence: 3, type: approval ? "approval_required" : "text_delta", data: approval ? { call_id: "call-one", tool_name: "node_health_remediate", approval: { command: "docker restart api", host: "fixture", action: "restart_service" } } : { content: "Working on fixture" } },
        { sequence: 4, type: "run_status", data: { status: approval ? "waiting_approval" : "running" } },
      ];
      if (approved) events.push({ sequence: 5, type: "tool_call_result", data: { call_id: "call-one", status: "success", result: { command_succeeded: true } } },
        { sequence: 6, type: "text_delta", data: { content: "Fixture completed after approval" } },
        { sequence: 7, type: "run_status", data: { status: "succeeded" } });
      if (cancelled) events.push({ sequence: 5, type: "run_status", data: { status: "cancelled" } });
      const body = events.filter(event => event.sequence > after).map(event => `id: ${event.sequence}\ndata: ${JSON.stringify({ ...event, run_id: "run-one" })}\n\n`).join("");
      return route.fulfill({ contentType: "text/event-stream", body });
    }
    if (path === "/api/runs/run-one") return json({ ...run(), messages: [{ role: "user", content: "Fixture request" }, ...(snapshotOnly ? [{ role: "assistant", content: "Restored final answer from snapshot" }] : [])], pending_approvals: [] });
    return json({ detail: "Fixture endpoint not found" }, 404);
  });
  return { failNextConnection: () => { reconnect = true; }, approvalCount: () => approvals };
}

async function send(page: Page) {
  await page.goto("/");
  await page.locator("textarea").fill("Fixture request");
  await page.locator("textarea").press("Enter");
}

test("reload and reconnect restore the run without duplicate text", async ({ page }) => {
  const backend = await fixture(page);
  await send(page);
  await expect(page.getByText("Working on fixture", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Working on fixture", { exact: true })).toHaveCount(1);
  backend.failNextConnection();
  await expect(page.getByRole("status")).toContainText("reconnecting", { timeout: 10000 });
  await expect(page.getByRole("status")).not.toContainText("reconnecting", { timeout: 10000 });
  await expect(page.getByText("Working on fixture", { exact: true })).toHaveCount(1);
});
test("approval resumes the same run exactly once", async ({ page }) => {
  const backend = await fixture(page, true);
  await send(page);
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("Fixture completed after approval", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("succeeded");
  expect(backend.approvalCount()).toBe(1);
});
test("Stop cancels the server run", async ({ page }) => {
  await fixture(page);
  await send(page);
  await page.getByRole("button", { name: "Stop run" }).click();
  await expect(page.getByRole("status")).toContainText("cancelled");
});

test("expired event retention restores the canonical snapshot", async ({ page }) => {
  await fixture(page, false, true);
  await send(page);
  await expect(page.getByText("Restored final answer from snapshot", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("succeeded");
});
