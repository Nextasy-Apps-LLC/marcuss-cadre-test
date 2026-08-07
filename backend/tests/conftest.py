"""Shared fixtures and SSE parsing helpers for the unit suite.

Every test here runs with `app.graph.models` monkeypatched to deterministic
verdicts. Phase 1b filled those seams with real Bedrock calls and this fixture
did not change shape — which is the seam paying off: the protocol tests that
proved routing and streaming offline still prove exactly the same things, and
no unit test can accidentally spend a Bedrock call.

`test_models.py` is where the real implementations are exercised, against a
scripted `app.llm.chat_model`.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.graph import models
from app.main import app

client = TestClient(app)


# --------------------------------------------------------------------------
# SSE parsing
# --------------------------------------------------------------------------

def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Split a raw SSE body into (event, payload) pairs, dropping comments."""
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        event = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def states(events: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """(step, status) pairs in wire order."""
    return [(p["step"], p["status"]) for e, p in events if e == "state"]


def detail_for(events: list[tuple[str, dict]], step: str, status: str) -> str | None:
    return next(
        p["detail"]
        for e, p in events
        if e == "state" and p["step"] == step and p["status"] == status
    )


def elapsed_for(events: list[tuple[str, dict]], step: str, status: str) -> int | None:
    return next(
        p["elapsed_ms"]
        for e, p in events
        if e == "state" and p["step"] == step and p["status"] == status
    )


def reply_text(events: list[tuple[str, dict]]) -> str:
    return "".join(p["text"] for e, p in events if e == "token")


def kinds(events: list[tuple[str, dict]]) -> list[str]:
    return [e for e, _ in events]


def ask(message: str, conversation_id: str | None = None, **extra):
    body = {
        "conversation_id": conversation_id or uuid.uuid4().hex[:16],
        "message": message,
        **extra,
    }
    return client.post("/ask", json=body, headers={"accept": "text/event-stream"})


def ask_events(message: str, **kwargs) -> list[tuple[str, dict]]:
    return parse_sse(ask(message, **kwargs).text)


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def seams(request, monkeypatch):
    """All model seams pass; individual tests override what they exercise.

    Tests marked `real_seams` opt out, so they can drive the real
    implementations with a scripted `app.llm.chat_model` instead.
    """
    if "real_seams" in request.keywords:
        return

    async def _pass(state):
        return models.Verdict("pass")

    async def _in_scope(state):
        return models.Verdict("in_scope")

    async def _reply(state):
        for part in ("Cadre AI ", "helps teams adopt AI ", "with senior guidance."):
            yield part

    monkeypatch.setattr(models, "validate_llm", _pass)
    monkeypatch.setattr(models, "judge_injection", _pass)
    monkeypatch.setattr(models, "classify_topic", _in_scope)
    monkeypatch.setattr(models, "guard_output", _pass)
    monkeypatch.setattr(models, "stream_reply", _reply)


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """The limiter is process-global; a shared bucket would make test order
    decide whether a later test is rate limited."""
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()
