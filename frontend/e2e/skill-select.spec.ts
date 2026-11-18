import { test, expect, type Page } from "@playwright/test";

const OPTIONS = [
  { name: "auto", kind: "auto", description: "The agent chooses" },
  { name: "alarm_statistics", kind: "builtin", description: "Alarm statistics" },
  { name: "custom_site_alarm_triage", kind: "custom", description: "Site triage", version: "1.0.0" },
];

async function fixture(page: Page, options = OPTIONS) {
  const bodies: Array<Record<string, unknown>> = [];
  const json = (route: { fulfill: (o: object) => Promise<void> }, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const run = (skill?: string) => ({ run_id: "run-one", conversation_id: "conversation-one", provider: "mock", skill: skill ?? "auto", status: "succeeded", sequence: 3, error_code: null, created_at: "2026-11-18T10:00:00", configuration: { candidates: [{ provider: "mock", model: "mock-model" }] } });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/config") return json(route, { providers: ["mock"] });
    if (path === "/api/conversations") return json(route, { conversations: [] });
    if (path === "/api/runs/options") return json(route, { skills: options });
    if (path === "/api/runs" && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      bodies.push(body);
      return json(route, run(body.skill), 202);
    }
    if (path.endsWith("/events")) return route.fulfill({ contentType: "text/event-stream", body: `id: 1\ndata: ${JSON.stringify({ run_id: "run-one", sequence: 1, type: "run_status", data: { status: "succeeded" } })}\n\n` });
    if (path.endsWith("/runs") && path.includes("/conversations/")) return json(route, { runs: [run("alarm_statistics")] });
    if (path === "/api/runs/run-one") return json(route, { ...run("alarm_statistics"), messages: [], pending_approvals: [], steps: [{ step_id: "s1", kind: "llm", status: "succeeded", started_at: "2026-11-18T10:00:00", finished_at: null, usage: { total_tokens: 42 } }], interventions: [{ id: "i1", message: "Check neighbours", status: "undelivered", created_at: "2026-11-18T10:00:01", delivered_at: null }] });
    return json(route, { detail: "Fixture endpoint not found" }, 404);
  });
  return bodies;
}

async function ask(page: Page, text: string) {
  await page.locator("textarea").fill(text);
  await page.locator("textarea").press("Enter");
}

test("auto omits skill and a chosen skill is sent exactly as selected", async ({ page }) => {
  const bodies = await fixture(page);
  await page.goto("/");
  const selector = page.getByLabel("Skill");
  await expect(selector).toHaveValue("auto");
  await ask(page, "First question");
  await expect.poll(() => bodies.length).toBe(1);
  expect("skill" in bodies[0]).toBe(false);
  await expect(page.locator("textarea")).toBeEnabled({ timeout: 10000 });
  await selector.selectOption("alarm_statistics");
  await ask(page, "Second question");
  await expect.poll(() => bodies.length).toBe(2);
  expect(bodies[1].skill).toBe("alarm_statistics");
});

test("the selector is hidden when the server offers no real choice", async ({ page }) => {
  await fixture(page, [OPTIONS[0]]);
  await page.goto("/");
  await expect(page.locator("textarea")).toBeVisible();
  await expect(page.getByLabel("Skill")).toHaveCount(0);
});

test("run history shows the pinned model, steps and intervention delivery", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  await ask(page, "Question");
  await page.getByText("Run history (1)").click();
  await page.getByRole("button", { name: /Open run run-one/ }).click();
  const detail = page.getByTestId("run-detail");
  await expect(detail).toContainText("1 step(s)");
  await expect(detail).toContainText('"total_tokens":42');
  await expect(page.getByLabel("Interventions")).toContainText("undelivered");
  await expect(page.getByLabel("Interventions")).toContainText("the run ended before it was applied");
  await expect(page.getByRole("button", { name: /Open run run-one/ })).toContainText("mock-model");
  await expect(page.getByRole("button", { name: /Open run run-one/ })).toContainText("alarm statistics");
});

test("a provider without a server key is shown as unavailable and cannot start a run", async ({ page }) => {
  const bodies = await fixture(page);
  await page.route("**/api/runs/options", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    skills: OPTIONS, providers: [{ id: "mock", model: "mock-model", available: false, reason: "No API key configured on the server" }] }) }));
  await page.goto("/");
  await expect(page.getByRole("alert").filter({ hasText: "unavailable" })).toBeVisible();
  await page.locator("textarea").fill("Question");
  await expect(page.locator("button").filter({ has: page.locator("svg.lucide-send") })).toBeDisabled();
  await page.locator("textarea").press("Enter");
  await expect.poll(() => bodies.length).toBe(0);
});

test("the pinned model of the selected provider is shown from the server", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/runs/options", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    skills: OPTIONS, providers: [{ id: "mock", model: "mock-model-7", available: true, reason: null }] }) }));
  await page.goto("/");
  await expect(page.getByText("mock-model-7")).toBeVisible();
});
