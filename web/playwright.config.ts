import { defineConfig, devices } from "@playwright/test";

/**
 * Frontend e2e regression suite — drives the real page in a real browser
 * against a real backend, `BASE_URL`-pointable. See `e2e/README.md`.
 *
 * `BASE_URL` defaults to `http://localhost:8088`, the vite dev server's own
 * port (`vite.config.ts`'s `server.port`), which already proxies `/ask`,
 * `/config` and `/healthz` to the local docker backend on :8080 — that
 * reproduces prod's same-origin shape (page + API on one hostname) for free,
 * with zero new proxy code. Point it at `https://cadre.marcuss.pro` to run
 * the identical suite against prod.
 */
export default defineConfig({
  testDir: "./e2e",
  // A live turn runs four-plus judge calls behind a single 55-60s budget
  // (CloudFront's origin-response cap, mirrored by the backend e2e suite's
  // TURN_BUDGET_S) — give the browser layer room on top of that rather than
  // racing it.
  timeout: 70_000,
  expect: { timeout: 10_000 },
  // Turns share one RATE_LIMIT_TURNS / RATE_LIMIT_WINDOW_S budget per client
  // id (backend/app/ratelimit.py). Running serially means the suite never has
  // to reason about concurrent turns tripping that limiter.
  fullyParallel: false,
  workers: 1,
  // A live-model turn is not perfectly deterministic (judge latency, the
  // occasional slow retrieval) — one retry in CI absorbs that without masking
  // a genuine miss twice. Zero retries locally so a real failure is loud.
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["github"]] : "list",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8088",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
