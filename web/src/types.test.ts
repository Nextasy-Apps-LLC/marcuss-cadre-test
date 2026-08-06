import { describe, expect, it } from "vitest";

import {
  formatLatency,
  freshRails,
  isInterestingReason,
  RAIL_ICONS,
  RAIL_SPECS,
} from "./types";

describe("formatLatency", () => {
  it("renders sub-second values in milliseconds", () => {
    expect(formatLatency(0)).toBe("0ms");
    expect(formatLatency(842.6)).toBe("843ms");
    expect(formatLatency(999)).toBe("999ms");
  });

  it("switches to seconds at one second", () => {
    expect(formatLatency(1000)).toBe("1.0s");
    expect(formatLatency(14210)).toBe("14.2s");
  });

  it("renders nothing when there is no measurement", () => {
    // A pending rail has no latency yet; "0ms" would be a lie.
    expect(formatLatency(null)).toBe("");
  });
});

describe("isInterestingReason", () => {
  it("suppresses reasons that restate the icon", () => {
    // "└─ on topic" under a green check is noise.
    for (const reason of ["ok", "on_topic", "clean", "response_ready"]) {
      expect(isInterestingReason(reason)).toBe(false);
    }
  });

  it("surfaces reasons that explain a refusal or a degradation", () => {
    expect(isInterestingReason("off_topic")).toBe(true);
    expect(isInterestingReason("service_degraded")).toBe(true);
    expect(isInterestingReason("unsafe")).toBe(true);
  });

  it("treats empty and null as uninteresting", () => {
    expect(isInterestingReason(null)).toBe(false);
    expect(isInterestingReason("")).toBe(false);
  });
});

describe("freshRails", () => {
  it("returns all six rails in execution order", () => {
    expect(freshRails().map((r) => r.id)).toEqual([
      "rail1",
      "rail2",
      "rail3",
      "rail4",
      "rail5",
      "rail6",
    ]);
  });

  it("starts every rail pending with no measurement", () => {
    for (const rail of freshRails()) {
      expect(rail.status).toBe("pending");
      expect(rail.latencyMs).toBeNull();
      expect(rail.reason).toBeNull();
    }
  });

  it("returns a fresh array each call", () => {
    // The hook calls this on every turn; a shared array would leak the
    // previous turn's verdicts into the next one.
    const first = freshRails();
    first[0]!.status = "blocked";
    expect(freshRails()[0]!.status).toBe("pending");
  });
});

describe("rail presentation", () => {
  it("gives every status a distinct icon", () => {
    const icons = Object.values(RAIL_ICONS);
    expect(new Set(icons).size).toBe(icons.length);
  });

  it("never renders a degraded rail with the passed icon", () => {
    // The whole point: a rail whose classifier failed must not look clean.
    expect(RAIL_ICONS.degraded).not.toBe(RAIL_ICONS.passed);
  });

  it("labels every rail", () => {
    for (const spec of RAIL_SPECS) {
      expect(spec.label.length).toBeGreaterThan(0);
    }
  });
});
