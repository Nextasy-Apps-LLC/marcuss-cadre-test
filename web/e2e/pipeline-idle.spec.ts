/**
 * The six pipeline step chips are static UI copy — `freshSteps()` paints all
 * six with client-side `STEP_LABELS` before any turn runs (`web/src/types.ts`),
 * so this needs no live model and no turn.
 */
import { test } from "@playwright/test";

import { expect, STEP_LABELS, STEP_ORDER, stepRow } from "./support";

test.describe("idle pipeline stepper", () => {
  test("every one of the six pipeline steps renders, in wire order, with its expected label", async ({ page }) => {
    await page.goto("/");

    const stepper = page.getByTestId("pipeline-stepper");
    await expect(stepper).toBeVisible();

    const rows = stepper.locator("[data-step]");
    await expect(rows).toHaveCount(STEP_ORDER.length);

    const order = await rows.evaluateAll((els) => els.map((el) => el.getAttribute("data-step")));
    expect(order).toEqual(STEP_ORDER);

    for (const step of STEP_ORDER) {
      const row = stepRow(page, step);
      await expect(row).toBeVisible();
      await expect(row.locator(".step-label")).toHaveText(STEP_LABELS[step]);
      // Nothing has run yet — every step starts pending, and pending steps
      // never carry a model label (freshSteps() before any /config-derived
      // send() closure has fired).
      await expect(row).toHaveAttribute("data-step-status", "pending");
    }
  });
});
