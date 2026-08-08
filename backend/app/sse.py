"""SSE wire format — protocol v2.

This module is the single source of truth for the contract with `web/`: the
pipeline stepper and the Vitest suite code against exactly these events and
exactly this `STEPS` order. Nothing imports across the boundary, so renaming a
field here compiles green on both sides and breaks silently in a browser
(KB-005) — `web/src/types.ts` mirrors this file verbatim and the two ship in
the same phase.

    event: trace  data: {trace_id, url}
    event: state  data: {step, status, detail, elapsed_ms}
    event: token  data: {text}
    event: done   data: {outcome, refusal_text}
    event: error  data: {message}
    : ping                                  (comment heartbeat, no data)

`done` is always the terminal event, except after `error`, which is terminal
on its own.
"""

from __future__ import annotations

import json
from typing import Literal

# The pipeline in execution order. The client paints one chip per entry up
# front and resolves them from `state` events, so this list and
# web/src/types.ts STEPS must agree.
STEPS: list[str] = [
    "validate_input",
    "injection_check",
    "topic_classifier",
    "retrieve",
    "brain",
    "output_safety",
]

Status = Literal["running", "pass", "fail", "skipped"]
Outcome = Literal["answered", "refused", "escalated", "error"]


def trace(trace_id: str, url: str) -> str:
    """The Langfuse trace for this turn.

    Emitted once, immediately, as the very first event of the turn — the trace
    id is generated locally, so the URL is known before the graph has started
    and the client can show the link for the whole turn rather than only after
    it settles.

    Never emitted when tracing is disabled or degraded: a visitor sees one fewer
    chip, never a broken turn (fail-open, and see `app/tracing.py`).
    """
    payload = {"trace_id": trace_id, "url": url}
    return f"event: trace\ndata: {json.dumps(payload)}\n\n"


def state(
    step: str, status: Status, detail: str | None = None, elapsed_ms: int | None = None
) -> str:
    """A pipeline transition.

    `detail` carries the machine-readable reason: the failing check
    (`off_topic`, `rate_limited`, …), `kb_not_wired` for a step that is not
    built yet, or `degraded` on a pass that came from the fail-open policy
    rather than a real verdict — a degraded pass renders amber, never green.

    `elapsed_ms` is always present on the wire but is only ever a real
    integer once the step has reached a terminal verdict (`pass`/`fail`); it
    is `null` for `running` and `skipped` — a step that hasn't finished, or
    never ran, has nothing to time, and reporting `0` would misleadingly
    imply a measurement was taken.
    """
    payload = {"step": step, "status": status, "detail": detail, "elapsed_ms": elapsed_ms}
    return f"event: state\ndata: {json.dumps(payload)}\n\n"


def token(text: str) -> str:
    """A fragment of the answer. Only emitted while `brain` runs or while the
    escalation text streams."""
    return f"event: token\ndata: {json.dumps({'text': text})}\n\n"


def done(outcome: Outcome, refusal_text: str | None = None) -> str:
    """The terminal event. `refusal_text` is what the client shows instead of
    whatever it has streamed so far when the outcome is `refused`."""
    payload = {"outcome": outcome, "refusal_text": refusal_text}
    return f"event: done\ndata: {json.dumps(payload)}\n\n"


def error(message: str) -> str:
    """A generic failure. Never a traceback: the 200 is already committed by
    the time anything can go wrong mid-stream, and details belong in the log."""
    return f"event: error\ndata: {json.dumps({'message': message})}\n\n"


def ping() -> str:
    """Comment frame. Keeps intermediaries from reaping a connection that is
    waiting on a slow step; clients drop comment frames."""
    return ": ping\n\n"


def unreported(reported: set[str] | frozenset[str]) -> list[str]:
    """Steps that never made it to the wire, in order.

    The server — not the client — decides what was skipped: on a terminal
    refusal or escalation these are emitted as `skipped` before `done`, so a
    stepper never has to infer anything from silence.
    """
    return [step for step in STEPS if step not in reported]
