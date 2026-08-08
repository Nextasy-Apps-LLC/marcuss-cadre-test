/**
 * Pure reducer over one chat turn's SSE v2 events.
 *
 * Kept separate from `useCadreChat` so the event-handling logic — the part
 * the acceptance criteria actually specify — is reachable by the node-env
 * vitest suite without a DOM. `useCadreChat` is a thin imperative wrapper:
 * it owns the fetch/AbortController/React-state side effects and calls these
 * functions to compute what the next turn state should be.
 */

import { freshSteps, type DoneEvent, type StateEvent, type StepName, type TokenEvent, type MessageStatus, type Outcome, type StepState } from "../types";

/** Shown when the wire `error` event fires or a dead connection leaves nothing else to say. */
export const ERROR_TEXT = "Something went wrong. Try again in a moment.";
/** Shown when the stream dies without `done` and nothing had streamed yet. */
export const OFFLINE_TEXT = "The chat is offline right now. Try again in a moment.";
/** Fallback when `done{outcome:"refused"}` omits `refusal_text`. */
export const DEFAULT_REFUSAL_TEXT = "Sorry — I can't answer that one.";
/** Shown when a turn is stopped before any text streamed. */
export const STOPPED_TEXT = "(stopped)";

export interface TurnState {
  steps: StepState[];
  replyText: string;
  replyStatus: MessageStatus;
  replyOutcome?: Outcome;
  /** Separates "ended cleanly" from "connection died" for the caller's finally-block. */
  sawDone: boolean;
}

export function freshTurn(models?: Partial<Record<StepName, string>>): TurnState {
  return { steps: freshSteps(models), replyText: "", replyStatus: "pending", sawDone: false };
}

export function applyState(turn: TurnState, event: StateEvent): TurnState {
  return {
    ...turn,
    steps: turn.steps.map((step) =>
      step.name === event.step
        ? {
            ...step,
            status: event.status,
            detail: event.detail ?? null,
            elapsedMs: event.elapsed_ms,
          }
        : step,
    ),
  };
}

export function applyToken(turn: TurnState, event: TokenEvent): TurnState {
  return { ...turn, replyText: turn.replyText + event.text, replyStatus: "streaming" };
}

/**
 * `done` is always terminal. Outcome drives how the buffered text finalizes:
 *  - `answered`  keeps the streamed text as-is.
 *  - `refused`   discards it for the server's `refusal_text` (falling back to
 *                default copy if omitted) — the output guard only sees a
 *                complete reply, so a refusal can arrive after tokens
 *                streamed, and the streamed text was only ever provisional.
 *  - `escalated` keeps the streamed text; `replyOutcome` is the distinct
 *                marker that tells it apart from a plain answer.
 *  - `error`     drops the buffer for the generic error copy.
 */
export function applyDone(turn: TurnState, event: DoneEvent): TurnState {
  const base: TurnState = { ...turn, sawDone: true, replyOutcome: event.outcome };

  switch (event.outcome) {
    case "answered":
      return { ...base, replyStatus: "done" };
    case "refused":
      return {
        ...base,
        replyText: event.refusal_text ?? DEFAULT_REFUSAL_TEXT,
        replyStatus: "done",
      };
    case "escalated":
      return { ...base, replyStatus: "done" };
    case "error":
      return { ...base, replyText: ERROR_TEXT, replyStatus: "error" };
  }
}

/** The wire's own `error {message}` frame — always terminal. */
export function applyError(turn: TurnState): TurnState {
  return { ...turn, sawDone: true, replyText: ERROR_TEXT, replyStatus: "error" };
}

/**
 * The stream ended without `done` and without an explicit stop — the
 * connection died mid-turn. Any step still `pending` has an unknown outcome:
 * amber `lost`, not a red `fail`. We genuinely do not know what it would
 * have said. A step already `running`/resolved when the stream died keeps
 * its last known state rather than being overwritten.
 */
export function applyStreamLost(turn: TurnState): TurnState {
  return {
    ...turn,
    steps: turn.steps.map((step) =>
      step.status === "pending" ? { ...step, status: "lost" } : step,
    ),
    replyText: turn.replyText || OFFLINE_TEXT,
    replyStatus: "error",
  };
}

/** The visitor hit stop; the fetch unwound via `AbortController`. */
export function applyAborted(turn: TurnState): TurnState {
  return { ...turn, replyText: turn.replyText || STOPPED_TEXT, replyStatus: "stopped" };
}
