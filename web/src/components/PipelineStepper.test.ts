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
import { freshSteps, type RetrievalInfo, type StepState, type TurnSummary } from "../types";

function render(retrieval: RetrievalInfo | null): string {
  const steps: StepState[] = freshSteps().map((step) =>
    step.name === "retrieve"
      ? { ...step, status: "pass", elapsedMs: 555, retrieval }
      : step,
  );
  return renderToStaticMarkup(
    createElement(PipelineStepper, {
      steps,
      open: true,
      onToggle: () => {},
      verbose: true,
      onVerboseToggle: () => {},
    }),
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

  it("keeps the step's detail and puts the elapsed time on the combined meta line", () => {
    const html = render({ query: null, hit_count: 6, top_score: 0.5319 });
    expect(html).toContain('data-testid="step-meta-retrieve"');
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

describe("PipelineStepper per-step usage rows (issue #109)", () => {
  const summary: TurnSummary = {
    latency_ms: 4363,
    tokens: { input: 10289, output: 164, total: 10453 },
    cost_usd: 0.00126,
    usage_source: "provider",
    cost_source: "model_prices",
    usage_tokens: {
      brain: { input: 10289, output: 164, total: 10453 },
      topic_classifier: { input: 2100, output: 0, total: 2100 },
    },
    step_cost_usd: { brain: 0.00126 },
  };

  function renderSteps(over: Partial<TurnSummary> = {}, stepsOver: Partial<StepState> = {}): string {
    const steps: StepState[] = freshSteps().map((step, i) =>
      i === 0 ? { ...step, ...stepsOver } : step,
    );
    return renderToStaticMarkup(
      createElement(PipelineStepper, {
        steps,
        summary: { ...summary, ...over },
        open: true,
        onToggle: () => {},
        verbose: true,
        onVerboseToggle: () => {},
      }),
    );
  }

  it("renders tokens and cost under a step the summary has usage for", () => {
    const html = renderSteps();
    expect(html).toContain('data-testid="step-usage-brain"');
    expect(html).toContain("~10.5k tokens · $0.00126");
  });

  it("renders tokens alone when the step is priced off the table", () => {
    const html = renderSteps();
    // topic_classifier has usage but no step_cost_usd entry → cost is omitted.
    expect(html).toContain('data-testid="step-usage-topic_classifier"');
    expect(html).toContain("~2.1k tokens");
    expect(html).not.toContain("~2.1k tokens · $");
  });

  it("renders no usage row for a step the summary has no numbers for", () => {
    const html = renderSteps();
    expect(html).not.toContain('data-testid="step-usage-retrieve"');
    expect(html).not.toContain('data-testid="step-usage-validate_input"');
    expect(html).not.toContain('data-testid="step-usage-output_safety"');
    expect(html).not.toContain('data-testid="step-usage-injection_check"');
  });

  it("renders no usage rows at all when the done event carried no summary", () => {
    const html = renderToStaticMarkup(
      createElement(PipelineStepper, {
        steps: freshSteps(),
        open: true,
        onToggle: () => {},
        verbose: true,
        onVerboseToggle: () => {},
      }),
    );
    expect(html).not.toContain("step-usage-");
  });

  it("joins timing and usage on one subordinate line when a step has both", () => {
    const steps: StepState[] = freshSteps().map((step) =>
      step.name === "brain" ? { ...step, status: "pass", elapsedMs: 4363 } : step,
    );
    const html = renderToStaticMarkup(
      createElement(PipelineStepper, {
        steps,
        summary,
        open: true,
        onToggle: () => {},
        verbose: true,
        onVerboseToggle: () => {},
      }),
    );
    // One `.step-meta` line carries both the elapsed time and the usage, not
    // two rows apart.
    expect(html).toContain('data-testid="step-meta-brain"');
    expect(html).toContain('data-testid="step-timing-brain"');
    expect(html).toContain("4363ms");
    expect(html).toContain('data-testid="step-usage-brain"');
    expect(html).toContain("~10.5k tokens · $0.00126");
  });
});

describe("PipelineStepper Verbose toggle and turn totals", () => {
  const summary: TurnSummary = {
    latency_ms: 4363,
    tokens: { input: 10289, output: 164, total: 10453 },
    cost_usd: 0.00126,
    usage_source: "provider",
    cost_source: "model_prices",
    usage_tokens: { brain: { input: 10289, output: 164, total: 10453 } },
    step_cost_usd: { brain: 0.00126 },
  };

  function render(verbose: boolean, withSummary: boolean): string {
    return renderToStaticMarkup(
      createElement(PipelineStepper, {
        steps: freshSteps().map((step, i) =>
          i === 0 ? { ...step, status: "pass" as const, elapsedMs: 22, retrieval: null } : step,
        ),
        summary: withSummary ? summary : undefined,
        open: true,
        onToggle: () => {},
        verbose,
        onVerboseToggle: () => {},
      }),
    );
  }

  it("renders a Verbose checkbox, on by default", () => {
    const html = render(true, false);
    expect(html).toContain('data-testid="stepper-verbose-toggle"');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('checked=""');
    expect(html).toContain("Verbose");
  });

  it("collapses each step to label, model, and status when Verbose is off", () => {
    const html = render(false, false);
    // validate_input ran (pass, 22ms) but every subordinate detail line is gone.
    expect(html).toContain('data-testid="step-validate_input"');
    expect(html).not.toContain("22ms");
    expect(html).not.toContain("step-meta-");
    expect(html).not.toContain("step-timing-");
    expect(html).not.toContain("step-usage-");
    expect(html).not.toContain("step-retrieval-");
    expect(html).not.toContain("step-detail");
  });

  it("hides the turn totals row when Verbose is off", () => {
    const html = render(false, true);
    expect(html).not.toContain('data-testid="step-total"');
    expect(html).not.toContain("~10.5k tokens");
  });

  it("shows the turn totals row at the end when Verbose is on", () => {
    const html = render(true, true);
    expect(html).toContain('data-testid="step-total"');
    expect(html).toContain("Total");
    expect(html).toContain("~10.5k tokens · $0.00126 · 4.4s");
  });

  it("omits the totals row when there is no summary even in Verbose mode", () => {
    const html = render(true, false);
    expect(html).not.toContain('data-testid="step-total"');
  });
});
