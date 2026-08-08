/**
 * Trace-link footnote coverage (issue #57).
 *
 * `Transcript` is presentational, so for once the assertion has to be about
 * actual rendered markup rather than pure logic. Per the issue's resolved
 * testing approach: no jsdom, no React Testing Library, no new dependency,
 * no `vite.config.ts` change — `react-dom/server`'s `renderToStaticMarkup`
 * renders a React tree to an HTML string in plain Node (it's part of
 * `react-dom`, already a runtime dependency), and this file stays `.ts`
 * (built with `React.createElement`, not JSX) so the existing
 * `include: ["src/**\/*.test.ts"]` vitest glob picks it up unmodified.
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Transcript } from "./Transcript";
import type { ChatMessage } from "../types";

const TRACE_NOTE_TEXT = "Traces can take up to 30 seconds to become reachable.";

function renderTranscript(messages: ChatMessage[]): string {
  return renderToStaticMarkup(createElement(Transcript, { messages }));
}

describe("Transcript trace-link footnote", () => {
  it("renders the footnote next to the trace link, associated via aria-describedby, when a trace event arrived", () => {
    const message: ChatMessage = {
      id: "reply-1",
      who: "cadre",
      text: "here you go",
      status: "done",
      traceUrl: "https://cloud.langfuse.com/trace/abc123",
    };

    const html = renderTranscript([message]);

    expect(html).toContain('data-testid="trace-link"');
    expect(html).toContain('data-testid="trace-note"');
    expect(html).toContain(TRACE_NOTE_TEXT);

    // The link must be associated with the footnote for screen readers,
    // not left as orphaned text: aria-describedby on the <a> must match the
    // footnote's own id.
    const describedByMatch = html.match(/aria-describedby="([^"]+)"/);
    expect(describedByMatch).not.toBeNull();
    const describedById = describedByMatch![1];
    expect(html).toContain(`id="${describedById}"`);

    // Sanity: the id the <a> points at is the same node carrying the footnote copy.
    const notePattern = new RegExp(
      `id="${describedById}"[^>]*data-testid="trace-note"[^>]*>${TRACE_NOTE_TEXT.replace(/\./g, "\\.")}`,
    );
    expect(html).toMatch(notePattern);
  });

  it("omits the footnote entirely when there is no trace event for the turn", () => {
    const message: ChatMessage = {
      id: "reply-2",
      who: "cadre",
      text: "here you go",
      status: "done",
      // no traceUrl — tracing was disabled/degraded for this turn
    };

    const html = renderTranscript([message]);

    expect(html).not.toContain('data-testid="trace-link"');
    expect(html).not.toContain('data-testid="trace-note"');
    expect(html).not.toContain(TRACE_NOTE_TEXT);
  });
});
