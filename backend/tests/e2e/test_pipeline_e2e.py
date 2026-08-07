"""e2e — the real image over real HTTP at `BASE_URL`.

Phase 1a scope: `/healthz`, `/config`, the deterministic refusal paths and the
SSE framing, because the model seams are still empty (Phase 1b fills them and
adds the answered-turn cases; Phase 1d grows this file into the full
scenario suite). What these prove that the unit suite cannot: the container
boots, uvicorn under the Lambda Web Adapter actually streams, and no proxy in
front of it buffers or rewrites the event stream.

    BASE_URL=http://localhost:8080 pytest -m e2e
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.e2e.conftest import parse_sse, post_ask_body

pytestmark = pytest.mark.e2e

# CloudFront caps an origin response at 60s and the Lambda timeout matches it
# (KB-004), so a turn that cannot finish inside the budget is a failure, not a
# slow success.
TURN_BUDGET_S = 55.0

STEPS = [
    "validate_input",
    "injection_check",
    "topic_classifier",
    "retrieve",
    "brain",
    "output_safety",
]


def ask(http, message, conversation_id=None, body=None):
    payload = body if body is not None else {
        "conversation_id": conversation_id or uuid.uuid4().hex[:16],
        "message": message,
    }
    raw, headers = post_ask_body(payload)
    started = time.monotonic()
    response = http.post("/ask", content=raw, headers=headers)
    elapsed = time.monotonic() - started
    assert elapsed < TURN_BUDGET_S, f"turn took {elapsed:.1f}s, budget is {TURN_BUDGET_S}s"
    return response


def states(events):
    return [(p["step"], p["status"]) for e, p in events if e == "state"]


def detail_for(events, step, status):
    return next(
        p["detail"]
        for e, p in events
        if e == "state" and p["step"] == step and p["status"] == status
    )


class TestSupportingEndpoints:
    def test_healthz(self, http):
        response = http.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_config(self, http):
        body = http.get("/config").json()
        assert body["greeting"]
        assert isinstance(body["suggestions"], list) and body["suggestions"]


class TestStreamFraming:
    def test_sse_headers_survive_the_hop(self, http):
        response = ask(http, "   ")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "no-cache" in response.headers["cache-control"]
        assert "no-transform" in response.headers["cache-control"]

    def test_the_body_is_not_length_delimited(self, http):
        # A Content-Length on an SSE response means something between here and
        # uvicorn buffered the whole stream before answering — which is exactly
        # the failure mode `AWS_LWA_INVOKE_MODE=response_stream` exists to
        # prevent, and it passes every unit test.
        with http.stream("POST", "/ask", **_stream_kwargs("   ")) as response:
            assert "content-length" not in response.headers
            frames = [line for line in response.iter_lines()]
        assert any(line.startswith("event: state") for line in frames)


class TestDeterministicRefusals:
    @pytest.mark.parametrize(
        "message,detail",
        [
            ("   ", "empty"),
            ("x" * 2001, "too_long"),
            ("bad\x00null", "control_chars"),
        ],
    )
    def test_input_refusals_arrive_as_sse_not_http_errors(self, http, message, detail):
        response = ask(http, message)
        assert response.status_code == 200
        events = parse_sse(response.text)
        assert detail_for(events, "validate_input", "fail") == detail
        assert events[-1][0] == "done"
        assert events[-1][1]["outcome"] == "refused"
        assert events[-1][1]["refusal_text"]

    def test_refused_turns_stream_no_tokens_and_skip_the_rest(self, http):
        events = parse_sse(ask(http, "   ").text)
        assert states(events) == [
            ("validate_input", "running"),
            ("validate_input", "fail"),
            *[(step, "skipped") for step in STEPS[1:]],
        ]
        assert not [e for e, _ in events if e == "token"]

    def test_a_non_json_body_is_refused_over_sse(self, http):
        response = ask(http, None, body="not json at all")
        assert response.status_code == 200
        events = parse_sse(response.text)
        assert detail_for(events, "validate_input", "fail") == "malformed_payload"
        assert events[-1][1]["outcome"] == "refused"

    def test_a_malformed_conversation_id_is_refused(self, http):
        events = parse_sse(ask(http, "hello", conversation_id="short").text)
        assert detail_for(events, "validate_input", "fail") == "malformed_payload"


class TestUnwiredSeams:
    """Phase 1a only.

    The seams raise `NotImplementedError`, so a turn that gets past validation
    exercises the fail-open policy (the judges degrade) and then the terminal
    error path (the brain has nothing to degrade to). Phase 1b replaces this
    with an answered turn — when it does, this test failing is the signal that
    the seams are live, not a regression.
    """

    def test_an_in_scope_turn_degrades_then_errors_without_leaking(self, http):
        response = ask(http, "What does Cadre AI do?")
        events = parse_sse(response.text)

        assert detail_for(events, "injection_check", "pass") == "degraded"
        assert detail_for(events, "topic_classifier", "pass") == "degraded"
        assert detail_for(events, "retrieve", "skipped") == "kb_not_wired"
        assert events[-1][0] == "error"
        assert set(events[-1][1]) == {"message"}
        for leak in ("Traceback", "NotImplementedError", "/var/task"):
            assert leak not in response.text


def _stream_kwargs(message: str) -> dict:
    raw, headers = post_ask_body(
        {"conversation_id": uuid.uuid4().hex[:16], "message": message}
    )
    return {"content": raw, "headers": headers}
