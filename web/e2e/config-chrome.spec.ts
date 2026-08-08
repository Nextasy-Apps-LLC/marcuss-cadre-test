/**
 * The greeting and suggestion chips render exactly what `/config` serves —
 * a chip the assistant would refuse, or a stale greeting, is the worst
 * possible first impression (backend/CLAUDE.md).
 *
 * Neither test in this file needs a live model: `/config` is a static
 * server-side read, and the assertions are about page chrome, not a turn.
 */
import { test } from "@playwright/test";

import { expect, fetchConfig } from "./support";

test.describe("config-served page chrome", () => {
  test("renders the /config greeting verbatim in the system message", async ({ page, request }) => {
    const config = await fetchConfig(request);

    await page.goto("/");

    // The greeting is the only `who: "system"` row in the transcript.
    const greeting = page.locator(".msg--system .msg-body");
    await expect(greeting).toBeVisible();

    // Exact equality, not "contains" or "non-empty": a stale/fallback
    // greeting is a present-but-wrong value, exactly the class of regression
    // this suite exists to catch.
    await expect(greeting).toHaveText(config.greeting);
  });

  test("renders every suggestion chip from /config, in order, with exact text", async ({ page, request }) => {
    const config = await fetchConfig(request);
    test.skip(config.suggestions.length === 0, "/config served no suggestions to compare against");

    await page.goto("/");

    const chips = page.getByTestId("suggested-prompt");
    await expect(chips).toHaveCount(config.suggestions.length);

    const labels = await chips.allTextContents();
    // Order-sensitive: a reordered-but-present set of chips is still wrong.
    expect(labels).toEqual(config.suggestions);
  });

  test("a chip's data-prompt attribute matches its visible label", async ({ page, request }) => {
    const config = await fetchConfig(request);
    test.skip(config.suggestions.length === 0, "/config served no suggestions to compare against");

    await page.goto("/");

    const chips = page.getByTestId("suggested-prompt");
    const count = await chips.count();
    for (let i = 0; i < count; i++) {
      const chip = chips.nth(i);
      const [label, dataPrompt] = await Promise.all([chip.textContent(), chip.getAttribute("data-prompt")]);
      expect(dataPrompt, `chip ${i} visible label ${JSON.stringify(label)}`).toBe(label);
    }
  });
});
