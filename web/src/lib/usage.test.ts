/**
 * Formatting for the per-turn aggregate the `done` event carries (issue #109).
 *
 * Pure logic, no JSX/DOM — reachable by the node-env vitest suite, matching
 * this repo's convention of keeping non-visual logic in `src/lib/`
 * (`web/CLAUDE.md`).
 *
 * The numbers come straight off the wire, which are the same numbers the
 * Langfuse trace records (the wire `summary` is `finalize_trace`'s return
 * value), so the transcript line and the trace cannot disagree.
 */
import { describe, expect, it } from "vitest";

import type { TurnSummary } from "../types";
import { formatCost, formatLatency, formatStepUsage, formatTokens, formatTurnSummary } from "./usage";

describe("formatTokens", () => {
  it("spells out counts under a thousand exactly", () => {
    expect(formatTokens(0)).toBe("0 tokens");
    expect(formatTokens(567)).toBe("567 tokens");
    expect(formatTokens(999)).toBe("999 tokens");
  });

  it("rounds thousands to one decimal with a tilde — the number is approximate", () => {
    expect(formatTokens(1000)).toBe("~1.0k tokens");
    expect(formatTokens(10453)).toBe("~10.5k tokens");
    expect(formatTokens(1499999)).toBe("~1500.0k tokens");
  });
});

describe("formatCost", () => {
  it("renders 4 significant digits with trailing zeros stripped", () => {
    expect(formatCost(0.00126)).toBe("$0.00126");
    expect(formatCost(0.001294)).toBe("$0.001294");
    expect(formatCost(0.00009)).toBe("$0.00009");
    expect(formatCost(1.2)).toBe("$1.2");
    expect(formatCost(0.1234)).toBe("$0.1234");
    expect(formatCost(999.99)).toBe("$1000");
  });

  it("writes tiny costs in full rather than leaking scientific notation", () => {
    // The retrieve embedding's per-step cost is ~$9.1e-7; toPrecision's
    // `$9.1e-7` is a real display wart in the stepper, so small values are
    // spelled out to 4 significant digits instead.
    expect(formatCost(9.1e-7)).toBe("$0.00000091");
    expect(formatCost(3e-5)).toBe("$0.00003");
  });

  it("renders a literal zero legibly", () => {
    expect(formatCost(0)).toBe("$0");
  });
});

describe("formatLatency", () => {
  it("renders milliseconds as seconds with one decimal", () => {
    expect(formatLatency(4363)).toBe("4.4s");
    expect(formatLatency(1200)).toBe("1.2s");
    expect(formatLatency(10)).toBe("0.0s");
  });
});

describe("formatTurnSummary", () => {
  const summary: TurnSummary = {
    latency_ms: 4363,
    tokens: { input: 10289, output: 164, total: 10453 },
    cost_usd: 0.00126,
    usage_source: "provider",
    cost_source: "model_prices",
    usage_tokens: {},
    step_cost_usd: {},
  };

  it("renders tokens, cost and latency on one line", () => {
    expect(formatTurnSummary(summary)).toBe("~10.5k tokens · $0.00126 · 4.4s");
  });

  it("drops the cost segment when nothing was priced", () => {
    expect(
      formatTurnSummary({ ...summary, cost_usd: 0, cost_source: "unpriced" }),
    ).toBe("~10.5k tokens · 4.4s");
  });

  it("drops the token and cost segments when usage is absent — only the real latency remains", () => {
    // A deterministic refusal never reaches the transport (KB-009 absent ≠
    // zero): rendering "0 tokens · $0" would imply a measurement that never
    // happened, so only latency survives.
    expect(
      formatTurnSummary({
        ...summary,
        tokens: { input: 0, output: 0, total: 0 },
        cost_usd: 0,
        usage_source: "absent",
        cost_source: "absent",
      }),
    ).toBe("4.4s");
  });
});

describe("formatStepUsage", () => {
  it("renders tokens and cost for a priced step", () => {
    expect(formatStepUsage(2100, 0.000123)).toBe("~2.1k tokens · $0.000123");
  });

  it("renders tokens alone when the step is unpriced", () => {
    expect(formatStepUsage(2100, undefined)).toBe("~2.1k tokens");
  });

  it("returns null when the summary has no usage for the step", () => {
    expect(formatStepUsage(undefined, undefined)).toBeNull();
  });
});
