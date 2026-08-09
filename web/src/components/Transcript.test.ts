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
import type { ChatMessage, TurnSummary } from "../types";

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

describe("Transcript citation rendering (issue #62)", () => {
  it("renders a real measured citation (bare URL alone on the final line) as a short labeled link, never as raw URL text", () => {
    // Verbatim shape from `prompts/context.txt`: the paragraph that used a
    // source ends, then a blank line, then the source's bare URL alone as
    // the entire content of the final line.
    const message: ChatMessage = {
      id: "reply-3",
      who: "cadre",
      text:
        "For document classification, the Claude Haiku tier is the most " +
        "appropriate choice. Haiku is optimized for high-volume tasks where " +
        "accuracy on straightforward inputs is more important than complex " +
        "reasoning. It is well-suited for document classification, data " +
        "extraction, and routing workflows.\n\n" +
        "https://www.cadreai.com/articles/ai-model-selection",
      status: "done",
    };

    const html = renderTranscript([message]);

    // The raw URL must never appear as visible text — only inside an href.
    expect(html).not.toContain(">https://www.cadreai.com/articles/ai-model-selection<");
    expect(html).toContain('href="https://www.cadreai.com/articles/ai-model-selection"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    // Short label, not the long URL, is the link's visible text.
    expect(html).toMatch(/<a[^>]*>see article<\/a>/);
    expect(html).not.toContain("dangerouslySetInnerHTML");
  });

  it("renders a /contact citation as a 'contact us' link and a /strategy citation as a 'see more' link", () => {
    const contactMsg: ChatMessage = {
      id: "reply-4",
      who: "cadre",
      text: "Reach out for pricing.\n\nhttps://www.cadreai.com/contact",
      status: "done",
    };
    const strategyMsg: ChatMessage = {
      id: "reply-5",
      who: "cadre",
      text: "Read more about our approach.\n\nhttps://www.cadreai.com/strategy",
      status: "done",
    };

    expect(renderTranscript([contactMsg])).toMatch(/<a[^>]*>contact us<\/a>/);
    expect(renderTranscript([strategyMsg])).toMatch(/<a[^>]*>see more<\/a>/);
  });

  it("does not linkify (renders as plain streaming text) while the message is still pending", () => {
    // Citations are only meaningful once the reply is settled — a partial,
    // still-streaming URL fragment must not be prematurely turned into a link.
    const message: ChatMessage = {
      id: "reply-6",
      who: "cadre",
      text: "partial text with https://www.cadreai.com/articles/ai-model",
      status: "pending",
    };

    const html = renderTranscript([message]);
    expect(html).not.toContain("<a ");
  });
});

describe("Transcript turn-summary line (issue #109)", () => {
  const summary: TurnSummary = {
    latency_ms: 4363,
    tokens: { input: 10289, output: 164, total: 10453 },
    cost_usd: 0.00126,
    usage_source: "provider",
    cost_source: "model_prices",
    usage_tokens: {},
    step_cost_usd: {},
  };

  it("renders the one-line aggregate under a settled cadre reply when the done event carried a summary", () => {
    const message: ChatMessage = {
      id: "reply-7",
      who: "cadre",
      text: "here you go",
      status: "done",
      summary,
    };

    const html = renderTranscript([message]);

    expect(html).toContain('data-testid="reply-summary"');
    expect(html).toContain("~10.5k tokens · $0.00126 · 4.4s");
  });

  it("omits the aggregate entirely when the done event carried no summary", () => {
    // Tracing was down/degraded for this turn — the wire had no `summary`
    // (backend fail-open), and the transcript must not invent one.
    const message: ChatMessage = {
      id: "reply-8",
      who: "cadre",
      text: "here you go",
      status: "done",
    };

    const html = renderTranscript([message]);
    expect(html).not.toContain('data-testid="reply-summary"');
    expect(html).not.toContain("tokens · $");
  });

  it("does not render a summary under a user message or a still-streaming reply", () => {
    const messages: ChatMessage[] = [
      { id: "you-1", who: "you", text: "hi", status: "done" },
      {
        id: "reply-9",
        who: "cadre",
        text: "partial",
        status: "streaming",
        summary,
      },
    ];

    const html = renderTranscript(messages);
    expect(html).not.toContain('data-testid="reply-summary"');
  });
});
