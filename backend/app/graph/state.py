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
        history=history,
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
