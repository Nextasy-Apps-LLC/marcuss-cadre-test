import { describe, expect, it } from "vitest";

import { freshSteps, isDegraded, stepIcon, STEP_ICONS, STEPS, type StepState } from "./types";

describe("STEPS", () => {
  it("matches the wire contract verbatim, in order", () => {
    // Contract from #24, verbatim: this array is the mirror boundary — a
    // name, an order change, or an added/removed step must change on both
    // sides in the same PR or neither.
    expect(STEPS).toEqual([
      "validate_input",
      "injection_check",
      "topic_classifier",
      "retrieve",
      "brain",
      "output_safety",
    ]);
  });
});

describe("freshSteps", () => {
  it("returns all six steps in wire order", () => {
    expect(freshSteps().map((s) => s.name)).toEqual(STEPS);
  });

  it("starts every step pending with no detail", () => {
    for (const step of freshSteps()) {
      expect(step.status).toBe("pending");
      expect(step.detail).toBeNull();
    }
  });

  it("labels every step with non-empty text", () => {
    for (const step of freshSteps()) {
      expect(step.label.length).toBeGreaterThan(0);
    }
  });

  it("returns a fresh array each call", () => {
    // Called at the start of every turn; a shared array would leak the
    // previous turn's verdicts into the next one.
    const first = freshSteps();
    first[0]!.status = "fail";
    expect(freshSteps()[0]!.status).toBe("pending");
  });
});

describe("STEP_ICONS", () => {
  it("gives every wire-visible + inferred status a distinct icon", () => {
    const icons = Object.values(STEP_ICONS);
    expect(new Set(icons).size).toBe(icons.length);
  });
});

describe("isDegraded / stepIcon", () => {
  function step(overrides: Partial<StepState>): StepState {
    return { name: "topic_classifier", label: "topic classifier", status: "pending", detail: null, ...overrides };
  }

  it("is true only for a `pass` whose detail is exactly \"degraded\"", () => {
    expect(isDegraded(step({ status: "pass", detail: "degraded" }))).toBe(true);
    expect(isDegraded(step({ status: "pass", detail: null }))).toBe(false);
    expect(isDegraded(step({ status: "fail", detail: "degraded" }))).toBe(false);
    expect(isDegraded(step({ status: "running", detail: "degraded" }))).toBe(false);
  });

  it("never renders a degraded step with the plain-pass icon", () => {
    // The whole point: a step whose verdict came from the fail-open policy
    // must not look like a clean pass.
    const degraded = stepIcon(step({ status: "pass", detail: "degraded" }));
    const passed = stepIcon(step({ status: "pass", detail: null }));
    expect(degraded).not.toBe(passed);
  });

  it("renders a plain pass with the ordinary pass icon otherwise", () => {
    expect(stepIcon(step({ status: "pass", detail: null }))).toBe(STEP_ICONS.pass);
  });
});
