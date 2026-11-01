import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e", timeout: 30000, retries: 0, workers: 1,
  use: { baseURL: "http://127.0.0.1:5173", headless: true, trace: "retain-on-failure" },
  webServer: { command: "npm run dev -- --host 0.0.0.0", url: "http://127.0.0.1:5173", reuseExistingServer: false },
  reporter: [["list"], ["html", { open: "never" }]],
});
