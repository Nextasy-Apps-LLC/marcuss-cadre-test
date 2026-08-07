import { describe, expect, it } from "vitest";

import { STEPS, type DoneEvent, type StateEvent, type StepName } from "../types";
import {
  applyAborted,
  applyDone,
  applyError,
  applyState,
  applyStreamLost,
  applyToken,
  DEFAULT_REFUSAL_TEXT,
  ERROR_TEXT,
  freshTurn,
  OFFLINE_TEXT,
  STOPPED_TEXT,
  type TurnState,
} from "./turnReducer";

function state(step: StepName, status: StateEvent["status"], detail?: string): StateEvent {
  return detail === undefined ? { step, status } : { step, status, detail };
}

describe("freshTurn", () => {
  it("starts every step pending with no detail, in wire order", () => {
    const turn = freshTurn();
    expect(turn.steps.map((s) => s.name)).toEqual(STEPS);
    for (const step of turn.steps) {
      expect(step.status).toBe("pending");
      expect(step.detail).toBeNull();
    }
    expect(turn.replyText).toBe("");
    expect(turn.replyStatus).toBe("pending");
    expect(turn.sawDone).toBe(false);
  });
});

describe("applyState", () => {
  it("only updates the named step, leaving the rest untouched", () => {
    const turn = freshTurn();
    const next = applyState(turn, state("injection_check", "running"));

    const byName = Object.fromEntries(next.steps.map((s) => [s.name, s]));
    expect(byName.injection_check!.status).toBe("running");
    for (const name of STEPS) {
      if (name !== "injection_check") expect(byName[name]!.status).toBe("pending");
    }
  });
});

describe("full happy path ordering", () => {
  it("walks every step running -> pass, then finalizes an answered reply", () => {
    let turn = freshTurn();

    for (const step of STEPS) {
      turn = applyState(turn, state(step, "running"));
      turn = applyState(turn, state(step, "pass"));
    }

    turn = applyToken(turn, { text: "hel" });
    turn = applyToken(turn, { text: "lo" });
    turn = applyDone(turn, { outcome: "answered" });

    expect(turn.steps.every((s) => s.status === "pass")).toBe(true);
    expect(turn.replyText).toBe("hello");
    expect(turn.replyStatus).toBe("done");
    expect(turn.replyOutcome).toBe("answered");
    expect(turn.sawDone).toBe(true);
  });
});

describe("fail terminals, including wire-authoritative skips", () => {
  it("applies a wire fail to the failing step", () => {
    let turn = freshTurn();
    turn = applyState(turn, state("injection_check", "fail", "prompt_injection"));

    const step = turn.steps.find((s) => s.name === "injection_check")!;
    expect(step.status).toBe("fail");
    expect(step.detail).toBe("prompt_injection");
  });

  it("applies wire-sent `skipped` directly — the client never infers it", () => {
    let turn = freshTurn();
    turn = applyState(turn, state("injection_check", "fail"));
    turn = applyState(turn, state("topic_classifier", "skipped"));
    turn = applyState(turn, state("retrieve", "skipped"));
    turn = applyState(turn, state("brain", "skipped"));
    turn = applyState(turn, state("output_safety", "skipped"));

    const byName = Object.fromEntries(turn.steps.map((s) => [s.name, s.status]));
    expect(byName.injection_check).toBe("fail");
    expect(byName.topic_classifier).toBe("skipped");
    expect(byName.retrieve).toBe("skipped");
    expect(byName.brain).toBe("skipped");
    expect(byName.output_safety).toBe("skipped");
    // validate_input never got an event — the reducer must not touch it.
    expect(byName.validate_input).toBe("pending");
  });
});

describe("degraded rendering state", () => {
  it("keeps a fail-open pass distinguishable from a clean pass via detail", () => {
    let turn = freshTurn();
    turn = applyState(turn, state("topic_classifier", "pass", "degraded"));

    const step = turn.steps.find((s) => s.name === "topic_classifier")!;
    expect(step.status).toBe("pass");
    expect(step.detail).toBe("degraded");
  });
});

describe("lost on dead stream", () => {
  it("marks only still-pending steps lost; steps already resolved keep their verdict", () => {
    let turn = freshTurn();
    turn = applyState(turn, state("validate_input", "pass"));
    turn = applyState(turn, state("injection_check", "running"));
    // topic_classifier .. output_safety never reported: still pending.

    turn = applyStreamLost(turn);

    const byName = Object.fromEntries(turn.steps.map((s) => [s.name, s.status]));
    expect(byName.validate_input).toBe("pass");
    // Only steps that never got any event are inferred lost; a step that was
    // mid-flight ("running") when the stream died is left as-is.
    expect(byName.injection_check).toBe("running");
    expect(byName.topic_classifier).toBe("lost");
    expect(byName.retrieve).toBe("lost");
    expect(byName.brain).toBe("lost");
    expect(byName.output_safety).toBe("lost");
  });

  it("falls back to the offline copy when nothing streamed yet", () => {
    const turn = applyStreamLost(freshTurn());
    expect(turn.replyText).toBe(OFFLINE_TEXT);
    expect(turn.replyStatus).toBe("error");
  });

  it("keeps whatever text had already streamed", () => {
    let turn = freshTurn();
    turn = applyToken(turn, { text: "partial" });
    turn = applyStreamLost(turn);
    expect(turn.replyText).toBe("partial");
    expect(turn.replyStatus).toBe("error");
  });
});

describe("refusal replacement", () => {
  it("discards the streamed buffer for the server's refusal_text", () => {
    let turn = freshTurn();
    turn = applyToken(turn, { text: "this was going somewhere" });
    turn = applyDone(turn, { outcome: "refused", refusal_text: "Not going to answer that." });

    expect(turn.replyText).toBe("Not going to answer that.");
    expect(turn.replyStatus).toBe("done");
    expect(turn.replyOutcome).toBe("refused");
  });

  it("falls back to default refusal copy when the wire omits refusal_text", () => {
    let turn = freshTurn();
    turn = applyToken(turn, { text: "partial" });
    turn = applyDone(turn, { outcome: "refused" });
    expect(turn.replyText).toBe(DEFAULT_REFUSAL_TEXT);
  });
});

describe("escalated outcome", () => {
  it("finalizes the streamed text and carries a distinct outcome marker", () => {
    let turn = freshTurn();
    turn = applyToken(turn, { text: "escalation-worthy reply" });
    turn = applyDone(turn, { outcome: "escalated" });

    // Unlike `refused`, the streamed text is kept — only the marker changes.
    expect(turn.replyText).toBe("escalation-worthy reply");
    expect(turn.replyStatus).toBe("done");
    expect(turn.replyOutcome).toBe("escalated");
    expect(turn.replyOutcome).not.toBe("answered");
  });
});

describe("sawDone terminal-path exhaustiveness", () => {
  const terminalStatuses = ["done", "stopped", "error"];

  it("done (every outcome) always resolves to done or error, never left pending/streaming", () => {
    const outcomes: DoneEvent[] = [
      { outcome: "answered" },
      { outcome: "refused", refusal_text: "no" },
      { outcome: "escalated" },
      { outcome: "error" },
    ];
    for (const event of outcomes) {
      const turn = applyDone(freshTurn(), event);
      expect(terminalStatuses).toContain(turn.replyStatus);
      expect(turn.sawDone).toBe(true);
    }
  });

  it("a wire error event resolves to error and is terminal", () => {
    const turn = applyError(freshTurn());
    expect(turn.replyStatus).toBe("error");
    expect(turn.sawDone).toBe(true);
  });

  it("a dead stream without done resolves to error", () => {
    const turn = applyStreamLost(freshTurn());
    expect(terminalStatuses).toContain(turn.replyStatus);
  });

  it("an aborted turn resolves to stopped, with (stopped) text when nothing streamed", () => {
    const turn = applyAborted(freshTurn());
    expect(turn.replyStatus).toBe("stopped");
    expect(turn.replyText).toBe(STOPPED_TEXT);
  });
});

describe("copy constants", () => {
  it("are non-empty, distinct strings", () => {
    const all = [OFFLINE_TEXT, ERROR_TEXT, DEFAULT_REFUSAL_TEXT, STOPPED_TEXT];
    for (const text of all) expect(text.length).toBeGreaterThan(0);
    expect(new Set(all).size).toBe(all.length);
  });
});

// Type-level smoke check: TurnState must carry everything the hook needs to
// drive both the message bubble and the stepper from one source of truth.
const _typeCheck: TurnState = freshTurn();
void _typeCheck;
