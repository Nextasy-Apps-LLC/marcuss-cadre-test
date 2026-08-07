"""The typed channel the graph carries from one node to the next.

Every terminal of the conversation is an explicit `outcome` here, and every
step that ran leaves a `StepResult` behind — the same facts the client is told
over SSE, so a trace and a transcript can never disagree.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Outcome = Literal["answered", "refused", "escalated", "error"]
Status = Literal["running", "pass", "fail", "skipped"]


class Turn(TypedDict):
    """One prior message in the conversation."""

    role: str
    text: str


# The server-side history budget (issue #43). `web/src/lib/history.ts` trims
# to the identical numbers before it ever sends a request, but that is a
# courtesy, not the enforcement: a client that ignores its own cap — broken,
# hand-crafted, or malicious — must not be able to inflate `brain`'s input
# past what fits inside CloudFront's 60s origin cap (KB-004) alongside four
# judge calls. Oversized history is truncated here, never refused; the visitor
# did nothing wrong, the server just keeps only what it can afford.
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 8000


def _truncate_history(history: list[Turn]) -> list[Turn]:
    """Most recent `MAX_HISTORY_TURNS` turns, then drop the oldest of those
    until the total text is at most `MAX_HISTORY_CHARS` — but never below the
    single most recent turn, even if it alone exceeds the budget. The mirror
    of `web/src/lib/history.ts`'s `buildHistory`; keep the two in lockstep."""
    recent = history[-MAX_HISTORY_TURNS:]

    start = 0
    total = sum(len(turn["text"]) for turn in recent)
    while total > MAX_HISTORY_CHARS and start < len(recent) - 1:
        total -= len(recent[start]["text"])
        start += 1
    return recent[start:]


class StepResult(TypedDict):
    step: str
    status: Status
    detail: str | None


class ConversationState(TypedDict, total=False):
    message: str
    history: list[Turn]
    client_id: str
    steps: list[StepResult]
    # Retrieved KB context. Reserved: the `retrieve` node fills it in Phase 3.
    context: str | None
    answer: str
    outcome: Outcome
    refusal_text: str | None


def initial_state(message: str, history: list[Turn], client_id: str) -> ConversationState:
    return ConversationState(
        message=message,
        history=_truncate_history(history),
        client_id=client_id,
        steps=[],
        context=None,
        answer="",
        outcome="answered",
        refusal_text=None,
    )


def reported(state: ConversationState) -> set[str]:
    """Steps that already told the client something."""
    return {step["step"] for step in state.get("steps", [])}


def last_step(state: ConversationState) -> StepResult | None:
    steps = state.get("steps", [])
    return steps[-1] if steps else None


def failed_step(state: ConversationState) -> str | None:
    """The step that refused the turn, if any."""
    for step in reversed(state.get("steps", [])):
        if step["status"] == "fail":
            return step["step"]
    return None
