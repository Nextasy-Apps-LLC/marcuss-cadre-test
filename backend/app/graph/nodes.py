"""One async function per pipeline step, all with the signature
`(state, emit) -> state`.

Two rules hold across every node here:

* **The wire is written before the state.** A node emits `running` before it
  works and its verdict as soon as it has one, so the stepper in the browser is
  live rather than a replay printed at the end of the turn.
* **Model-backed checks fail open.** A Bedrock outage degrades a verdict, it
  does not refuse a visitor: the pass is emitted with `detail:"degraded"` so
  the client can render it amber, and an outage that reads as green never
  happens. `brain` is the exception — there is no answer to degrade to, so a
  brain failure propagates and becomes a terminal `error`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from app import config, ratelimit, sse
from app.graph import models
from app.graph.state import ConversationState, StepResult, failed_step, reported

log = logging.getLogger("cadre.graph")

VALIDATE_INPUT = "validate_input"
INJECTION_CHECK = "injection_check"
TOPIC_CLASSIFIER = "topic_classifier"
RETRIEVE = "retrieve"
BRAIN = "brain"
OUTPUT_SAFETY = "output_safety"

DEGRADED = "degraded"

# Everything a terminal can print is machine-readable on the wire; these are
# the `detail` values `validate_input` can produce.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _elapsed_ms(started: float) -> int:
    """Milliseconds since `started` (a `time.monotonic()` timestamp), rounded
    to the nearest integer — never truncated, so a fast step under 1ms still
    reads as `0` rather than being pulled down by `int()` on a value like 0.6.
    """
    return round((time.monotonic() - started) * 1000)


async def _record(
    state: ConversationState,
    emit,
    step: str,
    status: str,
    detail: str | None = None,
    elapsed_ms: int | None = None,
) -> ConversationState:
    await emit(step, status, detail, elapsed_ms)
    result = StepResult(step=step, status=status, detail=detail)
    return {**state, "steps": [*state.get("steps", []), result]}


async def _skip_unreported(state: ConversationState, emit) -> ConversationState:
    """Server-authoritative skips.

    v1 left the client inferring which rails never ran; v2 says so explicitly,
    so a stepper never has to guess what silence means.
    """
    for step in sse.unreported(reported(state)):
        state = await _record(state, emit, step, "skipped")
    return state


def _chunks(text: str) -> list[str]:
    size = config.CHUNK_SIZE
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _stream_text(text: str, emit) -> None:
    for chunk in _chunks(text):
        await emit.token(chunk)
        # Hand control back so each event flushes as its own chunk rather than
        # coalescing into one write at the end.
        await asyncio.sleep(0)


def _validation_failure(state: ConversationState) -> str | None:
    """The first deterministic reason to refuse, or None.

    Deterministic on purpose, and first on purpose: a payload that a regex can
    reject must never cost a Bedrock call, and these are the checks that have
    to hold when Bedrock is unreachable. The model-backed half
    (`models.validate_llm`) only runs once all of these pass.
    """
    if not ratelimit.limiter.allow(state.get("client_id", "")):
        return "rate_limited"
    if not _CLIENT_ID.match(state.get("client_id", "")):
        return "malformed_payload"

    message = state.get("message", "")
    if not message.strip():
        return "empty"
    if len(message) > config.MAX_INPUT_LEN:
        return "too_long"
    if _CONTROL_CHARS.search(message):
        return "control_chars"
    return None


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

async def validate_input(state: ConversationState, emit) -> ConversationState:
    """Deterministic checks, then the SLM validity judge.

    Two halves, strictly ordered. The cheap half refuses malformed payloads
    without a network call; the model half catches gibberish that is
    structurally fine, and fails open like every other model-backed step.
    """
    started = time.monotonic()
    await emit(VALIDATE_INPUT, "running")

    detail = _validation_failure(state)
    if detail:
        return await _record(
            state, emit, VALIDATE_INPUT, "fail", detail, _elapsed_ms(started)
        )

    try:
        verdict = await models.validate_llm(state)
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("validate_input judge failed, passing degraded", exc_info=True)
        return await _record(
            state, emit, VALIDATE_INPUT, "pass", DEGRADED, _elapsed_ms(started)
        )

    if verdict.verdict == "fail":
        return await _record(
            state,
            emit,
            VALIDATE_INPUT,
            "fail",
            verdict.detail or "invalid",
            _elapsed_ms(started),
        )
    return await _record(
        state, emit, VALIDATE_INPUT, "pass", verdict.detail, _elapsed_ms(started)
    )


async def injection_check(state: ConversationState, emit) -> ConversationState:
    started = time.monotonic()
    await emit(INJECTION_CHECK, "running")
    try:
        verdict = await models.judge_injection(state)
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("injection_check seam failed, passing degraded", exc_info=True)
        return await _record(
            state, emit, INJECTION_CHECK, "pass", DEGRADED, _elapsed_ms(started)
        )

    if verdict.verdict == "fail":
        return await _record(
            state,
            emit,
            INJECTION_CHECK,
            "fail",
            verdict.detail or "injection",
            _elapsed_ms(started),
        )
    return await _record(
        state, emit, INJECTION_CHECK, "pass", verdict.detail, _elapsed_ms(started)
    )


async def topic_classifier(state: ConversationState, emit) -> ConversationState:
    started = time.monotonic()
    await emit(TOPIC_CLASSIFIER, "running")
    try:
        verdict = await models.classify_topic(state)
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("topic_classifier seam failed, passing degraded", exc_info=True)
        return await _record(
            state, emit, TOPIC_CLASSIFIER, "pass", DEGRADED, _elapsed_ms(started)
        )

    if verdict.verdict == "off_topic":
        return await _record(
            state, emit, TOPIC_CLASSIFIER, "fail", "off_topic", _elapsed_ms(started)
        )
    if verdict.verdict == "needs_human":
        # Not a failure: the classifier worked and routed the turn to a person.
        return await _record(
            state,
            emit,
            TOPIC_CLASSIFIER,
            "pass",
            "needs_human",
            _elapsed_ms(started),
        )
    return await _record(
        state, emit, TOPIC_CLASSIFIER, "pass", verdict.detail, _elapsed_ms(started)
    )


async def retrieve(state: ConversationState, emit) -> ConversationState:
    """Phase 3 fills this with condense → embed → LanceDB search.

    It reports `skipped` rather than staying silent so the client shows a
    resolved chip instead of one spinning for the rest of the turn.
    """
    return await _record(state, emit, RETRIEVE, "skipped", "kb_not_wired")


async def brain(state: ConversationState, emit) -> ConversationState:
    started = time.monotonic()
    await emit(BRAIN, "running")
    parts: list[str] = []
    async for delta in models.stream_reply(state):
        for chunk in _chunks(delta):
            parts.append(chunk)
            await emit.token(chunk)
            await asyncio.sleep(0)

    state = {**state, "answer": "".join(parts)}
    return await _record(state, emit, BRAIN, "pass", elapsed_ms=_elapsed_ms(started))


async def output_safety(state: ConversationState, emit) -> ConversationState:
    started = time.monotonic()
    await emit(OUTPUT_SAFETY, "running")
    try:
        verdict = await models.guard_output(state)
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        log.warning("output_safety seam failed, passing degraded", exc_info=True)
        state = await _record(
            state, emit, OUTPUT_SAFETY, "pass", DEGRADED, _elapsed_ms(started)
        )
        return {**state, "outcome": "answered"}

    if verdict.verdict == "fail":
        # The answer is already on the visitor's screen; `refuse` tells them to
        # drop it. Stream-then-retract is the deliberate trade-off in plan.md.
        return await _record(
            state,
            emit,
            OUTPUT_SAFETY,
            "fail",
            verdict.detail or "unsafe_output",
            _elapsed_ms(started),
        )

    state = await _record(
        state, emit, OUTPUT_SAFETY, "pass", verdict.detail, _elapsed_ms(started)
    )
    return {**state, "outcome": "answered"}


# --------------------------------------------------------------------------
# terminals
# --------------------------------------------------------------------------

async def refuse(state: ConversationState, emit) -> ConversationState:
    """Emits no tokens: what the visitor reads comes from `done.refusal_text`,
    which also replaces anything `brain` already streamed."""
    step = failed_step(state) or VALIDATE_INPUT
    state = await _skip_unreported(state, emit)
    return {
        **state,
        "outcome": "refused",
        "refusal_text": config.REFUSAL_TEXTS[step],
    }


async def escalate(state: ConversationState, emit) -> ConversationState:
    """Hands the turn to a human by streaming the booking link as tokens — the
    visitor reads it in the transcript like any other answer."""
    state = await _skip_unreported(state, emit)
    await _stream_text(config.ESCALATION_TEXT, emit)
    return {
        **state,
        "answer": config.ESCALATION_TEXT,
        "outcome": "escalated",
        "refusal_text": None,
    }
