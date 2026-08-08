"""SSE wire format — protocol v2.

This module is the single source of truth for the contract with `web/`: the
pipeline stepper and the Vitest suite code against exactly these events and
exactly this `STEPS` order. Nothing imports across the boundary, so renaming a
field here compiles green on both sides and breaks silently in a browser
(KB-005) — `web/src/types.ts` mirrors this file verbatim and the two ship in
the same phase.

    event: trace  data: {trace_id, url}
    event: state  data: {step, status, detail, elapsed_ms, retrieval}
    event: token  data: {text}
    event: done   data: {outcome, refusal_text}
    event: error  data: {message}
    : ping                                  (comment heartbeat, no data)

`done` is always the terminal event, except after `error`, which is terminal
on its own.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict

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


class Retrieval(TypedDict):
    """What `retrieve` actually searched for, and what came back.

    The two most diagnostic facts about a retrieval, and the same two the
    Langfuse span records. They are what separate "the answer is wrong" from
    "the *question* was wrong": once there is history the embedded text is a
    model's rewrite of the visitor's sentence, not the sentence itself.

    `query` is the **condensed** query and only when it differs from
    `state["message"]` — `None` on a first message (condensing never runs)
    and on the KB-011 fallback to the visitor's own words. Echoing a sentence
    that is already in the transcript back under the step is noise, and
    deciding it here keeps the raw message out of the client's stepper.

    `hit_count` and `top_score` describe the **final** slate — after the
    `RETRIEVE_MIN_SCORE` floor, the per-URL dedupe and the `RETRIEVE_TOP_K`
    cut — because that is the context the brain actually read. `top_score` is
    `None` exactly when `hit_count` is 0.

    Deliberately no chunk text and no URLs, same reason the Langfuse span
    omits the text: the passages are already in the brain's prompt, and
    duplicating them would make every frame expensive for no new fact.
    """

    query: str | None
    hit_count: int
    top_score: float | None


def retrieval(
    query: str | None, hit_count: int, top_score: float | None
) -> Retrieval:
    """Build a `Retrieval` payload. Lives here, not in the node, so the wire
    shape has exactly one definition to mirror in `web/src/types.ts`."""
    return {"query": query, "hit_count": hit_count, "top_score": top_score}


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
    step: str,
    status: Status,
    detail: str | None = None,
    elapsed_ms: int | None = None,
    retrieval: Retrieval | None = None,
) -> str:
    """A pipeline transition.

    `detail` carries the machine-readable reason: the failing check
    (`off_topic`, `rate_limited`, …), why a fail-open step gave up
    (`kb_unavailable`, `kb_disabled`, `kb_dimension_mismatch`, `kb_timeout`
    on `retrieve`; `kb_not_wired` was retired with the stub in #62), or
    `degraded` on a pass that came from the fail-open policy rather than a
    real verdict — a degraded pass renders amber, never green.

    New `detail` *values* are additive and need no client change: the web
    side reads `detail` as an opaque string. New *fields* would not be
    (KB-005).

    `elapsed_ms` is always present on the wire but is only ever a real
    integer once the step has reached a terminal verdict (`pass`/`fail`); it
    is `null` for `running` and `skipped` — a step that hasn't finished, or
    never ran, has nothing to time, and reporting `0` would misleadingly
    imply a measurement was taken.

    `retrieval` follows exactly that precedent: always present, `null`
    everywhere it does not apply — every step other than `retrieve`,
    `retrieve`'s own `running` frame, and every fail-open `skipped` path,
    where the search never completed and there is nothing to report. It is a
    `Retrieval` only on `retrieve`'s terminal `pass` (including `no_hits`).
    Always-present-or-null is what keeps the whole payload exact-matchable on
    both sides of the boundary rather than a shape that varies per step.
    """
    payload = {
        "step": step,
        "status": status,
        "detail": detail,
        "elapsed_ms": elapsed_ms,
        "retrieval": retrieval,
    }
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
