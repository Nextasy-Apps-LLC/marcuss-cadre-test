/**
 * What a visitor sees of the answer itself: incremental streaming with the
 * final text kept (never retracted for a clean `answered` outcome), citation
 * links rendered as short labelled links rather than raw URLs, and the
 * trace link with its ingestion-delay footnote.
 *
 * Every test needs a real answered turn, so the whole file is gated on
 * CADRE_E2E_BEDROCK.
 */
import { test } from "@playwright/test";

import { expect, lastReply, sendMessage, skipUnlessLive, waitForReplySettled } from "./support";

const LINK_LABELS = new Set(["see article", "contact us", "see more"]);

test.describe("answer rendering", () => {
  test("the answer streams incrementally and the final text is kept, not retracted", async ({ page }) => {
    skipUnlessLive(test);

    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");

    const reply = lastReply(page);
    const samples: number[] = [];
    const deadline = Date.now() + 60_000;

    while (Date.now() < deadline) {
      const status = await reply.getAttribute("data-status");
      if (status !== "streaming" && status !== "pending") break;
      const text = (await reply.locator(".msg-body").textContent()) ?? "";
      samples.push(text.length);
      await page.waitForTimeout(150);
    }

    await waitForReplySettled(page);
    const finalText = (await reply.locator(".msg-body").textContent()) ?? "";

    const distinctIncreasing = samples.filter((len, i) => i === 0 || len > samples[i - 1]);
    expect(
      distinctIncreasing.length,
      `expected at least two increasing text-length samples while streaming, saw ${JSON.stringify(samples)}`,
    ).toBeGreaterThanOrEqual(2);

    const maxSampled = samples.length ? Math.max(...samples) : 0;
    expect(
      finalText.length,
      `final text (${finalText.length} chars) is shorter than a streamed sample (${maxSampled} chars) — an answered turn must never shrink the visible text`,
    ).toBeGreaterThanOrEqual(maxSampled);
    expect(finalText.length).toBeGreaterThan(0);
  });

  test("citation links render as small labelled links, open in a new tab, and are not raw long URLs", async ({
    page,
  }) => {
    skipUnlessLive(test);

    await page.goto("/");
    // Mirrors backend/tests/e2e/test_pipeline_e2e.py's grounded citation case.
    await sendMessage(page, "How does Cadre AI choose LLMs and handle data security?");
    const reply = await waitForReplySettled(page);

    const links = reply.locator('.msg-body a:not([data-testid="trace-link"])');
    const count = await links.count();
    test.skip(count === 0, "this answer cited no cadreai.com link to assert against");

    for (let i = 0; i < count; i++) {
      const link = links.nth(i);
      const [href, target, rel, label] = await Promise.all([
        link.getAttribute("href"),
        link.getAttribute("target"),
        link.getAttribute("rel"),
        link.textContent(),
      ]);

      expect(href, `link ${i} href`).toMatch(/^https:\/\/(www\.)?cadreai\.com/);
      expect(target, `link ${i} target`).toBe("_blank");
      expect(rel ?? "", `link ${i} rel`).toContain("noopener");

      const trimmedLabel = (label ?? "").trim();
      expect(LINK_LABELS.has(trimmedLabel), `link ${i} label ${JSON.stringify(trimmedLabel)} is not one of ${[...LINK_LABELS]}`).toBe(
        true,
      );
      // A labelled link is short; a raw URL rendered as its own label is
      // exactly the regression a URL-extraction gotcha (KB-017) produces.
      expect(trimmedLabel).not.toBe(href);
      expect(trimmedLabel.length).toBeLessThan((href ?? "").length);
    }
  });

  test("the trace link renders with its 30-second footnote", async ({ page }) => {
    skipUnlessLive(test);

    await page.goto("/");
    await sendMessage(page, "What does Cadre AI do?");
    const reply = await waitForReplySettled(page);

    const traceLink = reply.getByTestId("trace-link");
    const count = await traceLink.count();
    test.skip(count === 0, "tracing was disabled/degraded for this turn — no trace event on the wire (fail-open)");

    await expect(traceLink).toBeVisible();
    const [href, target, rel, describedBy] = await Promise.all([
      traceLink.getAttribute("href"),
      traceLink.getAttribute("target"),
      traceLink.getAttribute("rel"),
      traceLink.getAttribute("aria-describedby"),
    ]);
    expect(href, "trace-link href").toBeTruthy();
    expect(target).toBe("_blank");
    expect(rel ?? "").toContain("noopener");
    expect(describedBy).toBeTruthy();

    // Do NOT resolve/open the trace URL itself — Langfuse Cloud ingestion is
    // async up to ~90s (KB-020); this suite asserts only that the link and
    // its footnote render, never that the trace is reachable yet.
    const note = reply.getByTestId("trace-note");
    await expect(note).toHaveText("Traces can take up to 30 seconds to become reachable.");
  });
});
