import { formatRetrievalQuery, formatRetrievalStats } from "../lib/retrieval";
import { isDegraded, stepIcon, type StepState } from "../types";

interface Props {
  steps: StepState[];
  /** Mobile only — the row list collapses behind the summary until tapped. */
  open: boolean;
  onToggle: () => void;
}

const STEPPER_ID = "pipeline-stepper-rows";

/**
 * The live guardrail pipeline.
 *
 * This is the point of the product, not decoration: a visitor watches each
 * step resolve in real time and can see *why* a turn was refused. It updates
 * incrementally as `state` events arrive rather than all at once at the end,
 * because a panel that fills in only after the answer proves nothing about
 * what actually ran.
 *
 * A single component (not a panel + a separate summary chip, as the v1 rail
 * trace was) so the container itself carries the mobile expand/collapse
 * affordance — CSS alone decides whether the summary trigger or the row list
 * is visible at a given breakpoint.
 */
export function PipelineStepper({ steps, open, onToggle }: Props) {
  const blocked = steps.some((step) => step.status === "fail");

  return (
    <aside
      className={`stepper${blocked ? " stepper--blocked" : ""}${open ? " stepper--open" : ""}`}
      aria-label="Guardrail pipeline"
      data-testid="pipeline-stepper"
    >
      <button
        type="button"
        className="stepper-summary"
        aria-expanded={open}
        aria-controls={STEPPER_ID}
        aria-label="Guardrail pipeline status — tap to expand"
        onClick={onToggle}
        data-testid="stepper-summary"
      >
        <span data-testid="stepper-summary-icons">
          {steps.map((step) => stepIcon(step)).join("")}
        </span>
      </button>

      <ul id={STEPPER_ID} className="stepper-rows" aria-label="Pipeline steps">
        {steps.map((step) => {
          const degraded = isDegraded(step);
          const variant = degraded ? "degraded" : step.status;
          // Non-null only on `retrieve`'s terminal pass — see
          // `StateEvent.retrieval`. `query` is null unless condensing
          // actually rewrote the visitor's question.
          const retrievalQuery = step.retrieval
            ? formatRetrievalQuery(step.retrieval.query)
            : null;

          return (
            <li
              key={step.name}
              className={`step step--${variant}`}
              data-step={step.name}
              data-step-status={step.status}
              data-testid={`step-${step.name}`}
            >
              <span className="step-icon" aria-hidden="true">
                {stepIcon(step)}
              </span>
              <span className="step-label">{step.label}</span>
              {step.model && (
                <span className="step-model" aria-label={`model: ${step.model}`}>
                  {step.model}
                </span>
              )}
              {/* Status is also text-borne, not only icon/color: the icon
                  above is aria-hidden, so this is the only accessible name
                  for a step's state. */}
              <span className="step-status-text">{degraded ? "degraded" : step.status}</span>

              {step.detail && !degraded && (
                <div className="step-detail">└─ {step.detail.replace(/_/g, " ")}</div>
              )}

              {/* What `retrieve` actually searched for and what came back.
                  Both are React text nodes: the query is derived from
                  visitor input via a model, and `dangerouslySetInnerHTML` is
                  banned repo-wide with no exception for "the model wrote
                  it". `formatRetrievalQuery` has already collapsed the
                  whitespace and capped the length. */}
              {retrievalQuery && (
                <div
                  className="step-detail step-retrieval-query"
                  data-testid="step-retrieval-query"
                >
                  └─ q: “{retrievalQuery}”
                </div>
              )}
              {step.retrieval && (
                <div className="step-detail" data-testid="step-retrieval-stats">
                  └─ {formatRetrievalStats(step.retrieval)}
                </div>
              )}

              {step.elapsedMs != null && (
                <span className="step-timing" data-testid={`step-timing-${step.name}`}>
                  {step.elapsedMs}ms
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
