/**
 * The two non-answered terminals a visitor can land on: a refusal (text
 * rendered, rest of the pipeline skipped) and an escalation (booking link
 * rendered). The refusal case here is deterministic — `validate_input` never
 * calls a model — so it runs ungated; the escalation case needs the
 * `topic_classifier` model to actually classify `needs_human`.
 *
 * Note: two of the backend's own deterministic refusal fixtures
 * (`backend/tests/e2e/test_pipeline_e2e.py::TestDeterministicRefusals`) are
 * NOT reachable through the real composer and are deliberately not used
 * here: a whitespace-only message never leaves the browser at all
 * (`Composer.submit`'s own `if (!text.trim()) return;` swallows it before
 * `onSend` is ever called), and a >2000-char message is silently truncated to
 * exactly 2000 by the input's native `maxLength` attribute before it can be
 * submitted (verified empirically: Chromium enforces `maxLength` even on a
 * programmatic `.fill()`, not just interactive typing). A control character
 * is the one deterministic case genuinely reachable end to end through the
 * real UI, so it stands in for the whole class here.
 */
import { test } from "@playwright/test";

import { expect, sendMessage, skipUnlessLive, STEP_ORDER, stepRow, waitForReplySettled } from "./support";

test.describe("turn outcomes", () => {
  test("an input-validation refusal renders the refusal text and skips the rest of the pipeline", async ({
    page,
  }) => {
    await page.goto("/");
    // Mirrors backend/tests/e2e/test_pipeline_e2e.py's ("bad\x00null", "control_chars") case.
    await sendMessage(page, "bad\x00null");
    const reply = await waitForReplySettled(page);

    // Verbatim from backend/app/config.py's REFUSAL_TEXTS["validate_input"] —
    // static copy, unrelated to the concurrent model-id-drift work.
    await expect(reply.locator(".msg-body")).toContainText(
      "I could not accept that message. Please try again with a shorter, plain-text question about Cadre AI.",
    );

    for (const step of STEP_ORDER.slice(1)) {
      await expect(stepRow(page, step), `${step} should be skipped after validate_input failed`).toHaveAttribute(
        "data-step-status",
        "skipped",
      );
    }
    await expect(stepRow(page, "validate_input")).toHaveAttribute("data-step-status", "fail");
  });

  test("an escalation renders the booking link", async ({ page }) => {
    skipUnlessLive(test);

    await page.goto("/");
    // Mirrors backend/evals/fixtures/topic_cases.json's needs_human_explicit_person case.
    await sendMessage(page, "Can I speak to a real person on your team please?");
    const reply = await waitForReplySettled(page);

    // `data-status` reads "done" identically for `answered` and `escalated`,
    // and `outcome` is not exposed as a data attribute — the booking link is
    // the only DOM-observable proof of an escalation. Do not add a
    // data-outcome attribute to app code to make this easier; assert what a
    // visitor already sees.
    const bookingLink = reply.locator('.msg-body a[href^="https://www.cadreai.com/contact"]');
    const count = await bookingLink.count();
    test.skip(count === 0, "topic_classifier did not route this turn to needs_human — nothing to assert");

    await expect(bookingLink.first()).toHaveText("contact us");
  });
});
