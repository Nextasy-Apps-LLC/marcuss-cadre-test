/**
 * The SSE contract, mirrored from the backend.
 *
 * These types are the integration surface between this app and `POST /ask`.
 * If a field name changes on one side it must change on the other — nothing
 * at build time can catch that drift, so the names here are deliberately
 * verbatim rather than prettified.
 */

export type RailId = "rail1" | "rail2" | "rail3" | "rail4" | "rail5" | "rail6";

export interface RailEvent {
  rail_id: RailId;
  rail_name: string;
  passed: boolean;
  latency_ms: number | null;
  reason: string;
  degraded: boolean;
}

export interface DoneEvent {
  refused: boolean;
  refusal_reason: string | null;
  latency_ms: number | null;
}

/**
 * `pending`  — not reached yet
 * `passed`   — clean
 * `degraded` — the rail's model call failed, so this verdict came from the
 *              fail-open policy rather than a real classification. Rendered
 *              amber, never green: an outage that reads as success is worse
 *              than a visible outage.
 * `blocked`  — the rail refused the turn
 * `skipped`  — an earlier rail blocked, so this one never ran
 * `lost`     — the stream died before this rail reported. Distinct from
 *              `blocked` on purpose; we do not know what it would have said.
 */
export type RailStatus =
  | "pending"
  | "passed"
  | "degraded"
  | "blocked"
  | "skipped"
  | "lost";

export interface RailState {
  id: RailId;
  name: string;
  label: string;
  status: RailStatus;
  latencyMs: number | null;
  reason: string | null;
}

export type MessageStatus = "pending" | "streaming" | "done" | "error" | "stopped";

export interface ChatMessage {
  id: string;
  who: "you" | "cadre" | "system";
  text: string;
  status: MessageStatus;
}

/** Rails in execution order, with the labels shown to a visitor. */
export const RAIL_SPECS: ReadonlyArray<{ id: RailId; name: string; label: string }> = [
  { id: "rail1", name: "input_validation", label: "input validation" },
  { id: "rail2", name: "injection", label: "injection check" },
  { id: "rail3", name: "topic", label: "topic classifier" },
  { id: "rail4", name: "brain", label: "brain" },
  { id: "rail5", name: "output_guard", label: "output safety" },
  { id: "rail6", name: "scrub", label: "output scrubber" },
];

export const RAIL_ICONS: Record<RailStatus, string> = {
  pending: "⏳",
  passed: "✅",
  degraded: "🟡",
  blocked: "🔴",
  skipped: "⏭",
  lost: "⚠️",
};

/**
 * Reasons that restate what the icon already says. Showing "└─ on topic" under
 * a green check is noise; showing "└─ off topic" under a red dot is the whole
 * point of the panel.
 */
const TRIVIAL_REASONS = new Set(["ok", "on_topic", "clean", "response_ready"]);

export function isInterestingReason(reason: string | null): reason is string {
  return reason !== null && reason !== "" && !TRIVIAL_REASONS.has(reason);
}

export function freshRails(): RailState[] {
  return RAIL_SPECS.map((spec) => ({
    ...spec,
    status: "pending" as RailStatus,
    latencyMs: null,
    reason: null,
  }));
}

/** `840ms`, `1.4s` — sub-second precision stops mattering above a second. */
export function formatLatency(ms: number | null): string {
  if (ms === null) return "";
  const rounded = Math.round(ms);
  return rounded < 1000 ? `${rounded}ms` : `${(rounded / 1000).toFixed(1)}s`;
}
