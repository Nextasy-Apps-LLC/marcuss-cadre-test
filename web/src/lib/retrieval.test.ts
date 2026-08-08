import { describe, expect, it } from "vitest";

import type { RetrievalInfo } from "../types";
import { formatRetrievalQuery, formatRetrievalStats, QUERY_MAX_CHARS } from "./retrieval";

function info(overrides: Partial<RetrievalInfo> = {}): RetrievalInfo {
  return { query: null, hit_count: 6, top_score: 0.5319, ...overrides };
}

describe("formatRetrievalQuery", () => {
  it("returns null when the backend reported no rewrite", () => {
    expect(formatRetrievalQuery(null)).toBeNull();
  });

  it("returns null for a blank or whitespace-only query", () => {
    expect(formatRetrievalQuery("")).toBeNull();
    expect(formatRetrievalQuery("   \n ")).toBeNull();
  });

  it("returns a short query unchanged apart from trimming", () => {
    expect(formatRetrievalQuery("  skip process mapping step  ")).toBe(
      "skip process mapping step",
    );
  });

  it("collapses newlines and runs of whitespace so the row stays one line", () => {
    expect(formatRetrievalQuery("skip process\nmapping    step")).toBe(
      "skip process mapping step",
    );
  });

  it("caps an over-long query at QUERY_MAX_CHARS including the ellipsis", () => {
    const long = "a".repeat(500);
    const out = formatRetrievalQuery(long)!;
    expect(out.length).toBe(QUERY_MAX_CHARS);
    expect(out.endsWith("…")).toBe(true);
  });

  it("does not truncate a query that is exactly at the cap", () => {
    const exact = "b".repeat(QUERY_MAX_CHARS);
    expect(formatRetrievalQuery(exact)).toBe(exact);
  });
});

describe("formatRetrievalStats", () => {
  it("says 0 hits when the corpus returned nothing above the floor", () => {
    expect(formatRetrievalStats(info({ hit_count: 0, top_score: null }))).toBe("0 hits");
  });

  it("uses the singular for exactly one hit", () => {
    expect(formatRetrievalStats(info({ hit_count: 1, top_score: 0.5319 }))).toBe(
      "1 hit · top 0.532",
    );
  });

  it("reports count and top score for a normal retrieval", () => {
    expect(formatRetrievalStats(info({ hit_count: 6, top_score: 0.5319 }))).toBe(
      "6 hits · top 0.532",
    );
  });

  it("omits the score when there is none, without inventing a zero", () => {
    expect(formatRetrievalStats(info({ hit_count: 3, top_score: null }))).toBe("3 hits");
  });
});
