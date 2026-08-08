/**
 * Formatting for the retrieval facts the `retrieve` step reports (issue #74).
 *
 * Pure logic, no JSX/DOM — reachable by the node-env vitest suite, matching
 * this repo's convention of keeping non-visual logic in `src/lib/`
 * (`web/CLAUDE.md`).
 *
 * The query these functions format is **derived from visitor input** (a
 * model's rewrite of the visitor's own sentence), so it is treated as
 * untrusted throughout: it is returned as a plain string for the caller to
 * render as a React text node, never as markup, and it is length-capped here
 * rather than left to CSS — a 300-character condensed query (the backend's
 * `CONDENSE_MAX_CHARS` ceiling) would otherwise be a paragraph inside a step
 * row. It is deliberately *not* run through `linkify`: this is a search
 * query, not model prose meant for the visitor to click.
 */

import type { RetrievalInfo } from "../types";

/**
 * Cap on the rendered query, ellipsis included. A search query only has to be
 * recognisable — long enough to tell "skip process mapping step in AI
 * implementation" from a rewrite that lost the question, short enough that
 * the step row stays a row.
 */
export const QUERY_MAX_CHARS = 80;

/** Decimal places on the score. The pane wants a magnitude, not the trace's precision. */
const SCORE_PRECISION = 3;

/**
 * The condensed query as one short line, or `null` when there is nothing
 * worth showing.
 *
 * `null` in means the backend judged the embedded text identical to the
 * visitor's own message (a first message, or the KB-011 fallback) — showing
 * a sentence that is already in the transcript back under the step is noise.
 * Whitespace is collapsed because a rewrite that came back with a newline in
 * it would otherwise break the row's single-line layout.
 */
export function formatRetrievalQuery(query: string | null): string | null {
  if (query === null) return null;

  const collapsed = query.replace(/\s+/g, " ").trim();
  if (collapsed === "") return null;
  if (collapsed.length <= QUERY_MAX_CHARS) return collapsed;

  return `${collapsed.slice(0, QUERY_MAX_CHARS - 1).trimEnd()}…`;
}

/**
 * The hit stats as one short line: `"6 hits · top 0.532"`, `"1 hit · top
 * 0.532"`, or `"0 hits"`.
 *
 * Zero is spelled out rather than suppressed: an empty corpus result and a
 * successful one must not look alike in the pane, which is exactly what PR
 * #63's reviewer flagged. The score is omitted entirely when there is none
 * — printing `0.000` would read as a real measurement of a very bad hit
 * rather than as the absence of any.
 */
export function formatRetrievalStats(info: RetrievalInfo): string {
  const noun = info.hit_count === 1 ? "hit" : "hits";
  const count = `${info.hit_count} ${noun}`;

  if (info.top_score === null) return count;
  return `${count} · top ${info.top_score.toFixed(SCORE_PRECISION)}`;
}
