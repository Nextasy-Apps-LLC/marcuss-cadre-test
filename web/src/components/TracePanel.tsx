import {
  formatLatency,
  isInterestingReason,
  RAIL_ICONS,
  type RailState,
} from "../types";

interface Props {
  rails: RailState[];
  totalMs: number | null;
  /** Mobile only — the panel is collapsed behind the summary chip. */
  open: boolean;
}

/**
 * The guardrail trace.
 *
 * This is the point of the product, not decoration: a visitor watches each
 * rail resolve in real time and can see *why* a turn was refused. It updates
 * incrementally as `rail` events arrive rather than all at once at the end,
 * because a panel that fills in only after the answer proves nothing about
 * what actually ran.
 */
export function TracePanel({ rails, totalMs, open }: Props) {
  const refused = rails.some((rail) => rail.status === "blocked");

  return (
    <aside
      id="trace-panel"
      className={`trace-panel${refused ? " trace-panel--refused" : ""}${open ? " trace-panel--open" : ""}`}
      aria-label="Guardrail trace"
      data-testid="trace-panel"
    >
      <div className="trace-title">
        <span>guardrail trace</span>
      </div>

      <ul className="trace-rows" aria-label="Guardrail rails">
        {rails.map((rail) => (
          <li
            key={rail.id}
            className={`rail rail--${rail.status}`}
            data-rail={rail.id}
            data-rail-name={rail.name}
            data-rail-status={rail.status}
            data-testid={`trace-rail-${rail.name}`}
          >
            <span className="rail-icon" aria-hidden="true">
              {RAIL_ICONS[rail.status]}
            </span>
            <span className="rail-label">{rail.label}</span>
            <span className="rail-latency">{formatLatency(rail.latencyMs)}</span>

            {isInterestingReason(rail.reason) && (
              <div
                className={`rail-reason${rail.status === "degraded" ? " rail-reason--warn" : ""}`}
              >
                └─ {rail.reason.replace(/_/g, " ")}
              </div>
            )}
          </li>
        ))}
      </ul>

      <div className="trace-footer">
        {totalMs !== null && (
          <span data-testid="trace-total">total: {formatLatency(totalMs)}</span>
        )}
      </div>
    </aside>
  );
}

/**
 * Mobile-only condensed view: the six rail icons as a single string.
 *
 * The full panel does not fit beside the terminal on a phone, but the live
 * signal still matters — the chip keeps it visible in one line and expands
 * the real panel on tap.
 */
export function TraceSummary({
  rails,
  totalMs,
  open,
  onToggle,
}: Props & { onToggle: () => void }) {
  const icons = rails.map((rail) => RAIL_ICONS[rail.status]).join("");

  return (
    <button
      type="button"
      className="trace-summary"
      aria-expanded={open}
      aria-controls="trace-panel"
      aria-label="Guardrail status — tap to expand"
      onClick={onToggle}
      data-testid="trace-summary"
    >
      <span data-testid="trace-summary-icons">{icons}</span>
      {totalMs !== null && (
        <span className="trace-summary-total">{formatLatency(totalMs)}</span>
      )}
    </button>
  );
}
