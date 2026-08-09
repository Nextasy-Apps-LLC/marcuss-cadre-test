/**
 * Formatting for the per-turn aggregate the `done` event carries (issue #109).
 *
 * Pure logic, no JSX/DOM — reachable by the node-env vitest suite, matching
 * this repo's convention of keeping non-visual logic in `src/lib/`
 * (`web/CLAUDE.md`).
 *
 * The numbers come straight off the wire, and the wire `summary` is
 * `finalize_trace`'s return value — the same dict written to the Langfuse
 * span — so the transcript line and the trace cannot disagree.
 *
 * The rendered strings are plain text for React text nodes, never markup:
 * they are numbers, but they arrive over an untyped wire and the repo bans
 * `dangerouslySetInnerHTML` with no exception.
 */

import type { TurnSummary } from "../types";

/**
 * Provider token count as one compact unit: exact under a thousand, one
 * decimal with a tilde above it (the number is approximate — rounded, not a
 * measurement the provider made at that precision).
 */
export function formatTokens(total: number): string {
  if (total < 1000) return `${total} tokens`;
  return `~${(total / 1000).toFixed(1)}k tokens`;
}

/**
 * USD as `$` with at most 4 significant digits and trailing zeros stripped.
 * A step's cost can be a fraction of a cent, so this never rounds to a fixed
 * number of decimal places (that would turn every per-step figure into
 * `$0.00`); 4 significant digits keeps the magnitude honest without
 * pretending to the provider's full precision.
 */
export function formatCost(usd: number): string {
  return `$${Number(usd.toPrecision(4)).toString()}`;
}

/** Turn latency in seconds with one decimal — the wire carries milliseconds. */
export function formatLatency(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * The one-line aggregate under a settled reply: `~10.5k tokens · $0.0013 ·
 * 4.4s`.
 *
 * Each segment is shown only when it is real (KB-009 absent ≠ zero): tokens
 * drop when there were none, cost drops when nothing was priced. Latency is
 * always real — the turn took it regardless of the transport — so it always
 * survives.
 */
export function formatTurnSummary(summary: TurnSummary): string {
  const parts: string[] = [];
  if (summary.tokens.total > 0) parts.push(formatTokens(summary.tokens.total));
  if (summary.cost_usd > 0) parts.push(formatCost(summary.cost_usd));
  parts.push(formatLatency(summary.latency_ms));
  return parts.join(" · ");
}

/**
 * One step's tokens (and cost, when the step was priced) as a short line:
 * `~2.1k tokens · $0.00012`. `null` when the summary has no usage for the
 * step — the caller renders nothing rather than a zeroed row.
 */
export function formatStepUsage(
  tokensTotal: number | undefined,
  costUsd: number | undefined,
): string | null {
  if (tokensTotal === undefined) return null;
  const parts: string[] = [formatTokens(tokensTotal)];
  if (costUsd !== undefined) parts.push(formatCost(costUsd));
  return parts.join(" · ");
}
