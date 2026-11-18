import { test, expect, type Page } from "@playwright/test";

const soon = new Date(Date.now() + 4 * 60000).toISOString().replace("Z", "");
const ITEMS = [
  { id: 1, conversation_id: "conv-x", conversation_title: "Restart api on node-a", run_id: "run-x", call_id: "c1", tool_name: "node_health_remediate",
    approval: { host: "node-a", service: "api", action: "restart_service", command: "docker restart api" }, expires_at: soon, actionable: true },
  { id: 2, conversation_id: "conv-y", conversation_title: "Other site", run_id: null, call_id: "c2", tool_name: "node_health_remediate",
    approval: { host: "node-z", service: "api", action: "restart_service" }, expires_at: soon, actionable: false },
];

async function fixture(page: Page, roles: string[]) {
  const opened: string[] = [];
  let inboxCalls = 0;
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/config") return json({ providers: ["mock"], actor: { subject: "alice", roles } });
    if (path === "/api/conversations") return json({ conversations: [] });
    if (path === "/api/runs/options") return json({ skills: [{ name: "auto", kind: "auto", description: "" }], providers: [] });
    if (path === "/api/approvals/pending") { inboxCalls++; return json({ approvals: ITEMS }); }
    if (path.endsWith("/messages")) { opened.push(path.split("/")[3]); return json({ conversation_id: "conv-x", provider: "mock", messages: [{ role: "user", content: "Restart api" }], pending_approvals: [] }); }
    if (path.endsWith("/runs") && path.includes("/conversations/")) return json({ runs: [] });
    return json({ detail: "Fixture endpoint not found" }, 404);
  });
  return { opened, inboxCalls: () => inboxCalls };
}

test("an approver sees live approvals with scope and expiry and can open one", async ({ page }) => {
  const backend = await fixture(page, ["approver"]);
  await page.goto("/");
  const inbox = page.getByLabel("Approval inbox");
  await expect(page.getByText("Approvals waiting (2)")).toBeVisible();
  await expect(inbox).toContainText("node-a / api");
  await expect(inbox).toContainText(/expires in [34] min/);
  await expect(inbox.locator("li").nth(1)).toContainText("outside your scope");
  await expect(inbox.locator("li").nth(0)).not.toContainText("outside your scope");
  await inbox.locator("li").nth(0).getByRole("button", { name: "Open" }).click();
  await expect.poll(() => backend.opened).toEqual(["conv-x"]);
  await expect(page.getByText("Restart api", { exact: true })).toBeVisible();
});

test("viewers and operators never see or request the inbox", async ({ page }) => {
  const backend = await fixture(page, ["viewer", "operator"]);
  await page.goto("/");
  await expect(page.locator("textarea")).toBeVisible();
  await expect(page.getByText(/Approvals waiting/)).toHaveCount(0);
  expect(backend.inboxCalls()).toBe(0);
});
