/**
 * Retrieval facts under the `retrieve` step (issue #74).
 *
 * Per KB-024, this asserts on real rendered JSX without jsdom or React
 * Testing Library: `react-dom/server`'s `renderToStaticMarkup` renders the
 * tree to an HTML string in plain Node, `react-dom` is already a runtime
 * dependency, and the file stays `.ts` (built with `React.createElement`,
 * not JSX) so the existing `include: ["src/**\/*.test.ts"]` vitest glob picks
 * it up with no `vite.config.ts` change.
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PipelineStepper } from "./PipelineStepper";
import { freshSteps, type RetrievalInfo, type StepState } from "../types";

function render(retrieval: RetrievalInfo | null): string {
  const steps: StepState[] = freshSteps().map((step) =>
    step.name === "retrieve"
      ? { ...step, status: "pass", elapsedMs: 555, retrieval }
      : step,
  );
  return renderToStaticMarkup(
    createElement(PipelineStepper, { steps, open: true, onToggle: () => {} }),
  );
}

const QUERY_TESTID = 'data-testid="step-retrieval-query"';
const STATS_TESTID = 'data-testid="step-retrieval-stats"';

describe("PipelineStepper retrieval facts", () => {
  it("renders the condensed query when the backend reported a rewrite", () => {
    const html = render({
      query: "skip process mapping step in AI implementation",
      hit_count: 6,
      top_score: 0.5319,
    });

    expect(html).toContain(QUERY_TESTID);
    expect(html).toContain("skip process mapping step in AI implementation");
  });

  it("renders no query line when condensing did not rewrite the question", () => {
    // `query: null` is the backend's way of saying the embedded text was the
    // visitor's own sentence — already in the transcript, so echoing it back
    // under the step is noise.
    const html = render({ query: null, hit_count: 6, top_score: 0.5319 });

    expect(html).not.toContain(QUERY_TESTID);
    expect(html).toContain(STATS_TESTID);
  });

  it("renders the hit count and top score", () => {
    const html = render({ query: null, hit_count: 6, top_score: 0.5319 });
    expect(html).toContain("6 hits · top 0.532");
  });

  it("renders 0 hits legibly rather than an empty success", () => {
    const html = render({ query: "does cadre sell hardware", hit_count: 0, top_score: null });

    expect(html).toContain(STATS_TESTID);
    expect(html).toContain("0 hits");
  });

  it("renders neither line when the step carries no retrieval payload", () => {
    // Every step other than `retrieve`, `retrieve`'s own `running` frame, and
    // every fail-open `skipped` path.
    const html = render(null);

    expect(html).not.toContain(QUERY_TESTID);
    expect(html).not.toContain(STATS_TESTID);
  });

  it("keeps the step's existing detail and timing untouched", () => {
    const html = render({ query: null, hit_count: 6, top_score: 0.5319 });
    expect(html).toContain('data-testid="step-timing-retrieve"');
    expect(html).toContain("555ms");
  });

  it("escapes a query carrying markup instead of injecting it", () => {
    // The condensed query is derived from visitor input via a model. It is
    // rendered as a React text node; `dangerouslySetInnerHTML` is banned
    // repo-wide and there is no exception for "the model wrote it".
    const html = render({
      query: '<img src=x onerror="alert(1)">',
      hit_count: 1,
      top_score: 0.9,
    });

    expect(html).not.toContain("<img");
    expect(html).not.toContain('onerror="alert(1)"');
    expect(html).toContain("&lt;img");
  });
});
