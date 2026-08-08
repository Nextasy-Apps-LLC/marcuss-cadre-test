/**
 * Shared helpers for the frontend e2e regression suite. Every wait lives
 * here so each spec file stays short and the one gotcha that has already
 * cost a debugging session (KB-018) has exactly one place to get right.
 */

import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/**
 * ## Tiering: the `@live` tag, not a skip-by-default env gate
 *
 * Specs that need a real model tag themselves `{ tag: "@live" }` and are
 * selected by `npm run test:e2e:live`; everything else is model-free and runs
 * via `npm run test:e2e` (`--grep-invert @live`), which is what CI runs on
 * every pull request.
 *
 * This replaced a `skipUnlessLive(test)` / `CADRE_E2E_BEDROCK` gate, and the
 * reason is the whole point of issue #97: that gate skipped **silently by
 * default**, so a run with the flag unset reported green while the
 * model-dependent half had never executed. Two known-red tests sat unfixed
 * across several merges and the bug was found by a human in a browser
 * instead. With tag selection the run's test count tells the truth — a tier
 * either ran or was never selected, and neither can masquerade as a pass.
 *
 * `CADRE_E2E_BEDROCK` keeps its original meaning for `backend/tests/e2e/`;
 * this change is scoped to the browser suite.
 */
export const LIVE_TAG = "@live";

export { expect };

/** The shape `GET /config` answers with — mirrors `App.tsx`'s `PageConfig`. */
export interface PageConfig {
  greeting: string;
  suggestions: string[];
  step_models: Record<string, string>;
}

/**
 * The single source of truth every spec compares rendered DOM text against.
 * No spec file may hardcode a model name/id, a config-sourced step label, or
 * the suggestion chip text — a concurrent fix to the backend's model roster
 * changes what this returns, and the suite must survive that unchanged.
 */
export async function fetchConfig(request: APIRequestContext): Promise<PageConfig> {
  const response = await request.get("/config");
  expect(response.ok(), `GET /config returned ${response.status()}`).toBeTruthy();
  return (await response.json()) as PageConfig;
}

/**
 * Pipeline steps in wire order, mirrored verbatim from `web/src/types.ts`'s
 * `STEPS` — the backend contract (`backend/app/sse.py` / `web/CLAUDE.md`).
 * Static, unrelated to the concurrent model-id-drift work.
 */
export const STEP_ORDER = [
  "validate_input",
  "injection_check",
  "topic_classifier",
  "retrieve",
  "brain",
  "output_safety",
] as const;

export type StepName = (typeof STEP_ORDER)[number];

/** Mirrored verbatim from `web/src/types.ts`'s `STEP_LABELS` — static UI copy. */
export const STEP_LABELS: Record<StepName, string> = {
  validate_input: "input validation",
  injection_check: "injection check",
  topic_classifier: "topic classifier",
  retrieve: "retrieve",
  brain: "brain",
  output_safety: "output safety",
};

/**
 * Drives the composer the way a real visitor does: fill, then Enter. The
 * composer's real submit `<button>` is intentionally `visually-hidden`
 * (`web/CLAUDE.md`) — Enter is the actual submit path, not a test shortcut
 * around one.
 */
export async function sendMessage(page: Page, text: string): Promise<void> {
  const input = page.getByTestId("chat-input");
  await input.fill(text);
  await input.press("Enter");
}

/** The most recent bot reply row. */
export function lastReply(page: Page): Locator {
  return page.locator(".msg--cadre").last();
}

/**
 * Waits for the BOT'S OWN reply to settle — never a bare `[data-status]`
 * selector.
 *
 * KB-018: `useCadreChat.send()` stamps the *visitor's own* message
 * `status: "done"` synchronously, alongside the bot's reply at `"pending"`.
 * A wait on a bare `[data-status="done"]` selector resolves instantly on the
 * user's own bubble — long before any state/token/done SSE event has
 * arrived — producing a false-positive "the turn settled" read against an
 * empty or half-rendered stepper. Scoping to `.msg--cadre` is what makes
 * this correct.
 */
export async function waitForReplySettled(page: Page, timeoutMs = 65_000): Promise<Locator> {
  const reply = lastReply(page);
  await expect(reply).toHaveAttribute("data-status", /^(done|error|stopped)$/, { timeout: timeoutMs });
  return reply;
}

/** One pipeline step's row in the stepper. */
export function stepRow(page: Page, step: StepName): Locator {
  return page.getByTestId(`step-${step}`);
}

/**
 * The per-step model labels the pane is currently painting, as a plain map
 * keyed by `data-step` — shaped to be compared to `/config`'s `step_models`
 * by deep equality.
 *
 * Deep equality over the whole map, rather than a label-by-label loop, is
 * deliberate: it is what makes a *wrong-but-present* label fail (the exact
 * regression class this suite exists for), and it fails just as loudly on a
 * chip that lost its label or gained an unexpected one. A step whose
 * `.step-model` span is absent contributes `null`, so a missing label can
 * never be mistaken for a matching one.
 *
 * Read through `expect.poll` — the labels are painted synchronously at send
 * time, but polling keeps the comparison from racing the first render.
 */
export async function renderedStepModels(page: Page): Promise<Record<string, string | null>> {
  return page.locator("[data-step]").evaluateAll((els) =>
    Object.fromEntries(
      els.map((el) => [el.getAttribute("data-step") ?? "", el.querySelector(".step-model")?.textContent ?? null]),
    ),
  );
}

export interface StepStatusLogEntry {
  step: string;
  status: string;
  t: number;
}

/**
 * Installs a MutationObserver on the `document` itself (see why below) that
 * records every `data-step-status` attribute change, before any app script
 * runs.
 *
 * MUST be called (and awaited) before `page.goto` — an observer attached
 * after the app has already mounted and painted the first `state` events
 * would miss the earliest transitions, and a settled-state snapshot alone
 * does not prove the pane actually updated live rather than all at once
 * (KB-007: a curl-green / single-snapshot check does not prove streaming).
 *
 * Observes `document` — NOT `document.body` and NOT `document.documentElement`.
 * `addInitScript` runs at the moment the new document is created, which is
 * before the HTML parser has produced *any* element node: both `document.body`
 * and `document.documentElement` are `null` at that instant, and
 * `observer.observe(null, ...)` throws `TypeError: parameter 1 is not of type
 * 'Node'` — silently discarding the whole observer, since the throw happens
 * inside `addInitScript`'s own isolated evaluation with nothing surfacing it
 * to the test (every run just came back an empty log). Confirmed empirically
 * against this exact app: `document` (the `Document` node) is the one target
 * that already exists at that instant, and `subtree: true` on it still covers
 * `<html>`, `<body>` and everything under them once the parser creates them.
 */
export async function installStepStatusRecorder(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { __stepStatusLog: StepStatusLogEntry[] }).__stepStatusLog = [];
    const observer = new MutationObserver((mutations) => {
      const log = (window as unknown as { __stepStatusLog: StepStatusLogEntry[] }).__stepStatusLog;
      for (const mutation of mutations) {
        if (mutation.type !== "attributes" || mutation.attributeName !== "data-step-status") continue;
        const el = mutation.target as HTMLElement;
        log.push({
          step: el.getAttribute("data-step") ?? "",
          status: el.getAttribute("data-step-status") ?? "",
          t: performance.now(),
        });
      }
    });
    observer.observe(document, {
      subtree: true,
      attributes: true,
      attributeFilter: ["data-step-status"],
    });
  });
}

export async function readStepStatusLog(page: Page): Promise<StepStatusLogEntry[]> {
  return page.evaluate(() => (window as unknown as { __stepStatusLog: StepStatusLogEntry[] }).__stepStatusLog ?? []);
}
