/**
 * The SSE v2 contract, mirrored from the backend.
 *
 * These types are the integration surface between this app and `POST /ask`.
 * If a field name or value changes on one side it must change on the other —
 * nothing at build time can catch that drift, so the names here are
 * deliberately verbatim rather than prettified.
 *
 * Wire events: `trace {trace_id, url}` (at most once, the first frame of the
 * turn, only when tracing is up — see `TraceEvent` below), `state {step,
 * status, detail, elapsed_ms, retrieval}`, `token {text}`, `done {outcome,
 * refusal_text?}` (always terminal), `error {message}` (terminal), and
 * `: ping` comment heartbeats (dropped in sse.ts, never reach this
 * boundary).
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

/**
 * What `retrieve` searched for and what came back — mirroring
 * `backend/app/sse.py`'s `Retrieval` TypedDict verbatim.
 *
 * `query` is the **condensed** query, and only when the backend judged it
 * different from the visitor's own message: `null` on a first message
 * (condensing never runs) and on the fallback where condensing gave up and
 * embedded the visitor's words. The decision is made server-side, so this
 * client never needs the raw message to know whether the query is worth
 * showing.
 *
 * `hit_count` and `top_score` describe the final slate the brain read —
 * after the score floor, the per-URL dedupe and the top-k cut. `top_score`
 * is `null` exactly when `hit_count` is 0.
 *
 * No chunk text and no URLs: the wire carries the count, the best score and
 * the query, nothing else.
 */
export interface RetrievalInfo {
  query: string | null;
  hit_count: number;
  top_score: number | null;
}

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
  /**
   * Retrieval facts, on exactly the same always-present/null-when-N/A terms
   * as `elapsed_ms` (KB-005): `null` for every step other than `retrieve`,
   * for `retrieve`'s own `running` frame, and for every fail-open
   * `skipped` path (`kb_unavailable`, `kb_timeout`, `kb_disabled`,
   * `kb_dimension_mismatch`) where the search never completed. Non-null
   * only on `retrieve`'s terminal `pass`, including the `no_hits` case —
   * which reports `hit_count: 0`, so an empty corpus result is legible
   * rather than looking like an empty success.
   */
  retrieval: RetrievalInfo | null;
}

export interface TokenEvent {
  text: string;
}

/**
 * The Langfuse trace for this turn (`backend/app/tracing.py` /
 * `app/sse.py`'s `trace()`). Emitted at most once per turn, as the very
 * first frame of the response, only when tracing is up — never emitted when
 * credentials are missing/bad (fail-open; KB-009). `url` is opaque, the
 * Langfuse SDK's own `get_trace_url()` output — never construct or parse it
 * client-side, just render it.
 */
export interface TraceEvent {
  trace_id: string;
  url: string;
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
  /** Mirrors `StateEvent.retrieval` verbatim — see its doc comment. */
  retrieval: RetrievalInfo | null;
  /**
   * Model id that runs this step, from `/config`'s `step_models` map.
   * Additive and generic — this type doesn't special-case any step name, so
   * `retrieve` picking up a model id (`embed-3-large`, issue #62) needed no
   * change here or in `useCadreChat`/`App`.
   */
  model?: string;
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
export function freshSteps(models?: Partial<Record<StepName, string>>): StepState[] {
  return STEPS.map((name) => ({
    name,
    label: STEP_LABELS[name],
    status: "pending" as StepStatus,
    detail: null,
    elapsedMs: null,
    retrieval: null,
    model: models?.[name],
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
  /**
   * Set the instant a `trace` event for this turn's reply arrives — before
   * the pipeline even starts, and independent of `status`/`outcome`. Absent
   * when tracing was disabled/degraded for this turn (no wire event at all).
   */
  traceUrl?: string;
}
