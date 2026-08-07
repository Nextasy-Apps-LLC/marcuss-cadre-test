"""The model seams.

Phase 1a builds the engine around these four functions and leaves them empty on
purpose: routing, streaming and the wire contract are provable offline with
them monkeypatched, and Phase 1b fills them in with `langchain-aws`
(`ChatBedrockConverse`) without touching a node, an edge or a test of the
protocol.

Nodes call these through the module (`models.judge_injection(...)`), never by
importing the name — that is what makes them patchable in one line.

Verdict vocabulary:
    judge_injection -> "pass" | "fail"
    classify_topic  -> "in_scope" | "off_topic" | "needs_human"
    guard_output    -> "pass" | "fail"
    stream_reply    -> async iterator of answer fragments
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from app.graph.state import ConversationState

_PHASE_1B = "wired in Phase 1b (Bedrock via langchain-aws)"


@dataclass(frozen=True)
class Verdict:
    """A step's decision plus the machine-readable reason put on the wire."""

    verdict: str
    detail: str | None = None


async def judge_injection(state: ConversationState) -> Verdict:
    raise NotImplementedError(f"judge_injection is {_PHASE_1B}")


async def classify_topic(state: ConversationState) -> Verdict:
    raise NotImplementedError(f"classify_topic is {_PHASE_1B}")


async def guard_output(state: ConversationState) -> Verdict:
    raise NotImplementedError(f"guard_output is {_PHASE_1B}")


def stream_reply(state: ConversationState) -> AsyncIterator[str]:
    """Answer fragments as the brain generates them.

    Deliberately a plain function returning an async iterator rather than an
    async generator: an unimplemented generator would raise only once someone
    iterated it, which is a far more confusing failure than raising at the call.
    """
    raise NotImplementedError(f"stream_reply is {_PHASE_1B}")
