/**
 * The SSE v2 contract, mirrored from the backend.
 *
 * These types are the integration surface between this app and `POST /ask`.
 * If a field name or value changes on one side it must change on the other —
 * nothing at build time can catch that drift, so the names here are
 * deliberately verbatim rather than prettified.
 *
 * Wire events: `state {step, status, detail, elapsed_ms}`, `token {text}`,
 * `done {outcome, refusal_text?}` (always terminal), `error {message}`
 * (terminal), and `: ping` comment heartbeats (dropped in sse.ts, never
 * reach this boundary).
 */

/** Pipeline steps, in the order the backend runs them. */
export const STEPS = [
  "validate_input",
  "injection_check",
  "topic_classifier",
  "retrieve",
  "brain",
  "output_safety",
] as const;

export type StepName = (typeof STEPS)[number];

/** Statuses a `state` event can carry on the wire. */
export type WireStepStatus = "running" | "pass" | "fail" | "skipped";

export interface StateEvent {
  step: StepName;
  status: WireStepStatus;
  detail?: string;
  /**
   * Milliseconds the step took, `round()`ed server-side — never truncated.
   * Always present on the wire (never omitted), per KB-005: `null` on
   * `running`/`skipped` (nothing measured yet, and a skipped step never ran
   * so `0` would misleadingly imply measurement); an integer `>= 0` on
   * `pass`/`fail`.
   */
  elapsed_ms: number | null;
}

export interface TokenEvent {
  text: string;
}

export type Outcome = "answered" | "refused" | "escalated" | "error";

export interface DoneEvent {
  outcome: Outcome;
  refusal_text?: string;
}

export interface ErrorEvent {
  message: string;
}

/**
 * One prior turn in the conversation, mirroring the backend's `Turn` model
 * (`backend/app/graph/state.py`) verbatim — `role`/`text`, unprettified.
 * Sent to `/ask` as `history: Turn[]`; the backend model already defaults
 * `history=[]`, so this is an additive field older clients simply omit
 * (KB-005-safe evolution).
 */
export interface Turn {
  role: "user" | "assistant";
  text: string;
}

/**
 * `pending`  — not reached yet. Client-inferred: the wire never sends it.
 * `running`  — currently executing.
 * `pass`     — clean pass. If `detail` is exactly `"degraded"` the verdict
 *              came from the fail-open policy rather than a real
 *              classification (same degraded-not-passed semantics as v1,
 *              now keyed off `detail` instead of a dedicated wire status) —
 *              render amber, never like an ordinary pass.
 * `fail`     — this step blocked the turn.
 * `skipped`  — server-authoritative: an earlier step blocked, so this one
 *              never ran. The wire sends this directly in v2; the client no
 *              longer infers it from a blocked-step index.
 * `lost`     — client-inferred: still `pending` when the stream died without
 *              `done`. Distinct from `fail` on purpose — we don't know what
 *              it would have said.
 */
export type StepStatus = "pending" | "running" | "pass" | "fail" | "skipped" | "lost";

export interface StepState {
  name: StepName;
  label: string;
  status: StepStatus;
  detail: string | null;
  /** Mirrors `StateEvent.elapsed_ms` verbatim — see its doc comment. */
  elapsedMs: number | null;
}

/** Human labels for the pipeline steps, in wire order. */
export const STEP_LABELS: Record<StepName, string> = {
  validate_input: "input validation",
  injection_check: "injection check",
  topic_classifier: "topic classifier",
  retrieve: "retrieve",
  brain: "brain",
  output_safety: "output safety",
};

export const STEP_ICONS: Record<StepStatus, string> = {
  pending: "⏳",
  running: "▶",
  pass: "✅",
  fail: "🔴",
  skipped: "⏭",
  lost: "⚠️",
};

/** Distinct from `STEP_ICONS.pass` — see the `StepStatus` doc comment. */
const DEGRADED_ICON = "🟡";

/** A `pass` whose verdict came from the fail-open policy, not a real classification. */
export function isDegraded(step: StepState): boolean {
  return step.status === "pass" && step.detail === "degraded";
}

export function stepIcon(step: StepState): string {
  return isDegraded(step) ? DEGRADED_ICON : STEP_ICONS[step.status];
}

/** Fresh steps for the start of a turn. A shared array would leak the previous turn's verdicts into the next one. */
export function freshSteps(): StepState[] {
  return STEPS.map((name) => ({
    name,
    label: STEP_LABELS[name],
    status: "pending" as StepStatus,
    detail: null,
    elapsedMs: null,
  }));
}

export type MessageStatus = "pending" | "streaming" | "done" | "error" | "stopped";

export interface ChatMessage {
  id: string;
  who: "you" | "cadre" | "system";
  text: string;
  status: MessageStatus;
  /**
   * Set only on a `done`-terminated reply. `escalated`'s distinct marker:
   * the message `status` stays `done` like `answered`, but `outcome` tells
   * the UI apart so it can render the escalation differently.
   */
  outcome?: Outcome;
}
