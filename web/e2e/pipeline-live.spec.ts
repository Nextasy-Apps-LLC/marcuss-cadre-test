/**
 * What the pipeline pane shows once a real, guarded turn has actually run:
 * model labels sourced from `/config` (never hardcoded — a concurrent fix to
 * the backend's model roster must not need this file to change), live
 * pending→running→terminal progression, per-step elapsed time, and the
 * retrieve step's condensed-query / hit-count lines (issue #74).
 *
 * Every test needs a real model behind the turn, so the whole file is gated
 * on CADRE_E2E_BEDROCK.
 */
import { test } from "@playwright/test";

import {
  expect,
  fetchConfig,
  installStepStatusRecorder,
  readStepStatusLog,
  LIVE_TAG,
  sendMessage,
  STEP_ORDER,
  stepRow,
  waitForReplySettled,
} from "./support";

test.describe("pipeline pane against a real turn", () => {
  test("every pipeline step chip carries its model label from /config, including on the very first turn of a session", { tag: LIVE_TAG }, async ({
    page,
    request,
  }) => {
    const config = await fetchConfig(request);

    // A fresh Playwright context/page means fresh localStorage — no prior
    // conversation id, so this really is turn one of a session (the exact
    // scenario the first-turn stale-closure bug reproduces in).
    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");
    await waitForReplySettled(page);

    for (const step of STEP_ORDER) {
      const expected = config.step_models[step];
      test.skip(!expected, `/config did not serve a model for ${step}`);
      const label = stepRow(page, step).locator(".step-model");
      await expect(label, `${step}'s model label`).toHaveText(expected);
    }
  });

  test("step chips progress from pending through running to a terminal status, and none is left running once the turn ends", { tag: LIVE_TAG }, async ({
    page,
  }) => {
    await installStepStatusRecorder(page);
    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");
    await waitForReplySettled(page);

    const log = await readStepStatusLog(page);
    expect(log.length, "no data-step-status mutations were recorded at all").toBeGreaterThan(0);

    const finalStatusByStep = new Map<string, string>();
    for (const entry of log) finalStatusByStep.set(entry.step, entry.status);

    for (const [step, finalStatus] of finalStatusByStep) {
      if (finalStatus === "skipped") continue; // a skipped step never ran, so it never reports "running"
      const sawRunning = log.some((e) => e.step === step && e.status === "running");
      expect(sawRunning, `${step} went straight to ${finalStatus} without ever reporting running`).toBe(true);
    }

    // Settled means every step reached a terminal status — none left spinning.
    const runningRows = page.locator('[data-step-status="running"]');
    await expect(runningRows).toHaveCount(0);
  });

  test("every step that actually ran shows its elapsed time; a step the server never ran shows none", { tag: LIVE_TAG }, async ({
    page,
  }) => {
    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");
    await waitForReplySettled(page);

    for (const step of STEP_ORDER) {
      const row = stepRow(page, step);
      const status = await row.getAttribute("data-step-status");
      const timing = page.getByTestId(`step-timing-${step}`);

      if (status === "pass" || status === "fail") {
        await expect(timing, `${step} (${status}) should show elapsed time`).toBeVisible();
        await expect(timing).toHaveText(/^\d+ms$/);
      } else if (status === "skipped") {
        await expect(timing, `${step} (skipped) should show no elapsed time`).toHaveCount(0);
      }
    }
  });

  test("the retrieve step shows a hit-count line on a passing retrieval, and no condensed-query line on a first message", { tag: LIVE_TAG }, async ({
    page,
  }) => {
    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");
    await waitForReplySettled(page);

    const retrieve = stepRow(page, "retrieve");
    const status = await retrieve.getAttribute("data-step-status");
    test.skip(status !== "pass", `retrieve ended ${status}, not pass — nothing to assert about hit stats`);

    // Rendered as `└─ {stats}` (PipelineStepper.tsx), same subordinate-line
    // convention as `.step-detail` — the glyph prefix is part of the text.
    await expect(page.getByTestId("step-retrieval-stats")).toHaveText(/^└─ (\d+ hits? · top 0\.\d+|0 hits)$/);
    // Condensing never runs on a first message (no history to condense
    // against) — the query line must be absent, not blank.
    await expect(page.getByTestId("step-retrieval-query")).toHaveCount(0);
  });

  test("a genuine multi-turn follow-up that gets condensed shows the condensed query", { tag: LIVE_TAG }, async ({ page }) => {
    await page.goto("/");
    await sendMessage(page, "What is the AI Maturity Index?");
    await waitForReplySettled(page);

    const followUp = "How do I get scored on that?";
    await sendMessage(page, followUp);
    await waitForReplySettled(page);

    const retrieve = stepRow(page, "retrieve");
    const status = await retrieve.getAttribute("data-step-status");
    test.skip(status !== "pass", `retrieve ended ${status}, not pass — nothing to assert about the condensed query`);

    const queryLine = page.getByTestId("step-retrieval-query");
    // Hard assertion, not `test.skip(count === 0, …)`. This is the one test
    // covering the condensed query Marcus reported missing (#97), and a
    // skip-when-absent escape hatch means it would have reported *skipped*
    // — not failed — on exactly the regression it exists to catch. A
    // follow-up whose subject is a bare pronoun ("that") cannot be embedded
    // usefully without condensing, so an absent line here is a real defect,
    // not model nondeterminism; this spec is in the `@live` tier, where the
    // config's single CI retry absorbs genuine flake.
    await expect(queryLine, "no condensed-query line rendered for a genuine follow-up").toHaveCount(1);

    const text = await queryLine.textContent();
    expect(text, "condensed-query line was empty").toBeTruthy();
    expect(text, "condensed-query line just echoed the raw follow-up").not.toContain(followUp);
  });
});
