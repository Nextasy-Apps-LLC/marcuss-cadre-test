/**
 * Everything the pipeline pane paints on a turn that needs **no model at
 * all** — the tier that runs on every pull request.
 *
 * The turn driven here is the control-character message `"bad\x00null"`,
 * which `validate_input` rejects without a single model call (mirroring
 * `backend/tests/e2e/test_pipeline_e2e.py`'s own `("bad\x00null",
 * "control_chars")` fixture, the same one `outcomes.spec.ts` uses).
 *
 * Why that is enough to cover the per-step model labels — the point of this
 * file: the labels come from `freshTurn(stepModels)` in `useCadreChat.send`
 * at **send time**, before a single SSE frame arrives. They are `/config`
 * data painted onto the chips by the client, not something the wire reports.
 * So the first-turn stale-closure bug (issue #97) reproduces on *any* first
 * turn, including a deterministic, free one — which is what lets the
 * regression that hit prod be caught on every PR at zero Bedrock cost,
 * instead of behind an opt-in flag nobody sets.
 *
 * Model-dependent assertions (streamed text, citations, the trace link,
 * retrieval stats, the condensed query, escalation) live in the `@live`
 * tier — see `web/e2e/README.md`.
 */
import { test } from "@playwright/test";

import {
  expect,
  fetchConfig,
  installStepStatusRecorder,
  readStepStatusLog,
  renderedStepModels,
  sendMessage,
  STEP_ORDER,
  stepRow,
  waitForReplySettled,
} from "./support";

/**
 * Rejected by `validate_input` on a pure `str` scan, no model involved. A
 * whitespace-only or >2000-char message cannot stand in: the composer blocks
 * blank client-side and Chromium enforces the input's native `maxLength`
 * even on a programmatic `.fill()` (see `outcomes.spec.ts`).
 */
const DETERMINISTIC_REFUSAL = "bad\x00null";

test.describe("pipeline pane on a deterministic, model-free turn", () => {
  test("every step chip carries its model label from /config on the very first turn of a session", async ({
    page,
    request,
  }) => {
    const config = await fetchConfig(request);

    // A roster that is empty or missing a step must FAIL here, never quietly
    // skip: a per-step `test.skip(!expected)` escape hatch would let this
    // whole assertion evaporate exactly when `/config` regressed, which is
    // the class of bug the suite exists to catch.
    expect(
      Object.keys(config.step_models).sort(),
      "/config did not serve a model for every pipeline step",
    ).toEqual([...STEP_ORDER].sort());
    for (const [step, model] of Object.entries(config.step_models)) {
      expect(model, `/config served an empty model label for ${step}`).toBeTruthy();
    }

    // A fresh Playwright context means fresh localStorage — no prior
    // conversation id, so this really is turn one of a session, the exact
    // scenario the stale-closure bug reproduces in.
    await page.goto("/");
    await sendMessage(page, DETERMINISTIC_REFUSAL);
    await waitForReplySettled(page);

    // Deep equality over the WHOLE rendered map, not label-by-label: this is
    // what gives a wrong-but-present label teeth. A mislabeled chip, a chip
    // that lost its label, and an extra chip all fail this one assertion,
    // and it can never pass against a hardcoded expectation — the right-hand
    // side is what `/config` answered at test-run time.
    await expect
      .poll(() => renderedStepModels(page), {
        message: "rendered per-step model labels did not match /config's step_models",
      })
      .toEqual(config.step_models);
  });

  test("step chips progress from pending through a terminal status, and none is left running", async ({
    page,
  }) => {
    // KB-007: installed before `page.goto` so the observer sees the earliest
    // transitions. A settled-state snapshot cannot tell "the pane updated
    // live" from "the pane filled in all at once at the end".
    await installStepStatusRecorder(page);
    await page.goto("/");
    await sendMessage(page, DETERMINISTIC_REFUSAL);
    await waitForReplySettled(page);

    const log = await readStepStatusLog(page);
    // Every chip renders `pending` on first paint, so any recorded mutation
    // is proof the pane moved *after* that paint — it was updated live, not
    // handed to the browser pre-settled (KB-007).
    expect(log.length, "no data-step-status mutations were recorded at all").toBeGreaterThan(0);

    const finalStatusByStep = new Map<string, string>();
    for (const entry of log) finalStatusByStep.set(entry.step, entry.status);

    expect(finalStatusByStep.get("validate_input")).toBe("fail");
    // Only ever `running` or `fail` — never back to `pending`, never a
    // status the server did not send.
    for (const entry of log.filter((e) => e.step === "validate_input")) {
      expect(["running", "fail"], `validate_input reported ${entry.status}`).toContain(entry.status);
    }

    // NOT asserted here: that a `running` frame was actually *painted* for
    // `validate_input`. Verified empirically against this backend — the
    // deterministic refusal resolves in `elapsed_ms: 0`, so its `running`
    // and `fail` state events arrive in the same network chunk and React 18
    // batches both `setSteps` calls into one commit; the DOM legitimately
    // never holds the intermediate value. That is a property of a
    // zero-duration step, not a defect, and asserting it here would be
    // asserting something the app is not obliged to paint. The
    // observable-`running` assertion belongs to — and stays in —
    // `pipeline-live.spec.ts`, where steps take real time.
    for (const step of STEP_ORDER.slice(1)) {
      expect(finalStatusByStep.get(step), `${step} after a validate_input failure`).toBe("skipped");
    }

    await expect(page.locator('[data-step-status="running"]')).toHaveCount(0);
  });

  test("the step that ran shows its elapsed time; the steps the server skipped show none", async ({
    page,
  }) => {
    await page.goto("/");
    await sendMessage(page, DETERMINISTIC_REFUSAL);
    await waitForReplySettled(page);

    await expect(stepRow(page, "validate_input")).toHaveAttribute("data-step-status", "fail");
    const timing = page.getByTestId("step-timing-validate_input");
    await expect(timing, "validate_input (fail) should show elapsed time").toBeVisible();
    await expect(timing).toHaveText(/^\d+ms$/);

    for (const step of STEP_ORDER.slice(1)) {
      await expect(
        page.getByTestId(`step-timing-${step}`),
        `${step} (skipped) should show no elapsed time`,
      ).toHaveCount(0);
    }
  });
});
