"""End-to-end tests for POST /ask.

These drive the real ASGI app through Starlette's TestClient, so they cover
request validation, the rail sequence, chunked token emission, and the SSE
framing together — the seams where a change on one side silently breaks the
browser client.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import STUB_REPLY, app

client = TestClient(app)


def ask(message: str, conversation_id: str | None = None):
    return client.post(
        "/ask",
        json={
            "conversation_id": conversation_id or uuid.uuid4().hex[:16],
            "message": message,
        },
        headers={"accept": "text/event-stream"},
    )


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Split a raw SSE body into (event, payload) pairs."""
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
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if data:
            events.append((event, json.loads(data)))
    return events


def reply_text(events: list[tuple[str, dict]]) -> str:
    return "".join(p["text"] for e, p in events if e == "token")


class TestPingPong:
    def test_ping_answers_pong(self):
        response = ask("ping")
        assert response.status_code == 200
        assert reply_text(parse_sse(response.text)) == "pong"

    @pytest.mark.parametrize("message", ["PING", "  ping  ", "Ping"])
    def test_ping_is_case_and_whitespace_insensitive(self, message):
        assert reply_text(parse_sse(ask(message).text)) == "pong"

    def test_anything_else_gets_the_stub(self):
        assert reply_text(parse_sse(ask("what is cadre?").text)) == STUB_REPLY

    def test_ping_inside_a_sentence_is_not_a_ping(self):
        # Substring matching here would make "don't ping me" answer "pong".
        assert reply_text(parse_sse(ask("should I ping the server?").text)) == STUB_REPLY


class TestSseContract:
    def test_content_type_is_event_stream(self):
        assert ask("ping").headers["content-type"].startswith("text/event-stream")

    def test_response_is_not_cached(self):
        # A cached SSE response is a stream that never streams. `no-transform`
        # additionally stops proxies from buffering to re-encode.
        cache_control = ask("ping").headers["cache-control"]
        assert "no-cache" in cache_control
        assert "no-transform" in cache_control

    def test_all_six_rails_are_emitted_in_order(self):
        events = parse_sse(ask("ping").text)
        rail_ids = [p["rail_id"] for e, p in events if e == "rail"]
        assert rail_ids == ["rail1", "rail2", "rail3", "rail4", "rail5", "rail6"]

    def test_rails_precede_tokens(self):
        # The client paints the trace panel from rail events; if tokens landed
        # first the panel would still be blank when the answer appeared.
        order = [e for e, _ in parse_sse(ask("ping").text)]
        assert order.index("rail") < order.index("token")

    def test_stream_terminates_with_done(self):
        events = parse_sse(ask("ping").text)
        assert events[-1][0] == "done"
        assert events[-1][1]["refused"] is False

    def test_rail_events_carry_the_full_shape(self):
        rail = next(p for e, p in parse_sse(ask("ping").text) if e == "rail")
        assert set(rail) == {
            "rail_id",
            "rail_name",
            "passed",
            "latency_ms",
            "reason",
            "degraded",
        }

    def test_reply_arrives_in_multiple_token_events(self):
        # Exercises the client's incremental-render path. A single-chunk reply
        # would let a broken token handler pass unnoticed.
        events = parse_sse(ask("what is cadre?").text)
        assert len([e for e, _ in events if e == "token"]) > 1

    def test_done_reports_a_latency(self):
        _, payload = parse_sse(ask("ping").text)[-1]
        assert payload["latency_ms"] >= 0


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"message": "ping"},                                  # no conversation_id
            {"conversation_id": "abcdefgh"},                      # no message
            {"conversation_id": "short", "message": "ping"},      # id too short
            {"conversation_id": "abcdefgh", "message": "   "},    # blank message
            {"conversation_id": "abcdefgh", "message": "x" * 2001},
            {"conversation_id": "abcdefgh", "message": "bad\x00null"},
        ],
    )
    def test_malformed_requests_are_refused_not_crashed(self, body):
        # Refusals travel as a normal SSE stream, not an HTTP error, so the
        # browser client renders them through its existing `done` path rather
        # than falling into its offline branch.
        response = client.post("/ask", json=body)
        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[-1][0] == "done"
        assert events[-1][1]["refused"] is True

    def test_refusal_names_the_blocking_rail(self):
        response = client.post("/ask", json={"conversation_id": "abcdefgh", "message": ""})
        assert parse_sse(response.text)[-1][1]["refusal_reason"].startswith("rail1:")


class TestSupportingEndpoints:
    def test_healthz(self):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_config_advertises_a_working_suggestion(self):
        # A chip that gets refused is the worst possible first impression, so
        # every advertised prompt must actually be answerable.
        body = client.get("/config").json()
        assert body["greeting"]
        for prompt in body["suggestions"]:
            assert reply_text(parse_sse(ask(prompt).text)) == "pong"
