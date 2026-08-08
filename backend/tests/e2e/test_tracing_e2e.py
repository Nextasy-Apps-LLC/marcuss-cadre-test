"""e2e — Langfuse traceability against a real target with real credentials.

## The `CADRE_E2E_LANGFUSE` gate

Same rationale as `CADRE_E2E_BEDROCK`, for the same reason and with the same
shape: tracing is **fail-open**. A target whose Langfuse credentials are
missing or wrong does not fail a "was a trace emitted?" test — it degrades,
emits no `trace` event at all, and a suite that only asserted "the turn still
completed" would go green against a service that has silently stopped being
observable (KB-009). So a human has to assert that this target is supposed to
be traced; "no trace event, must be disabled, skip" is exactly the reasoning
that lets a broken deploy through.

The target needs `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
`LANGFUSE_HOST` in its environment (Terraform injects them in Lambda; locally
they go on `docker run -e`). This suite itself needs none of them — it only
reads the wire and then fetches the advertised URL as an anonymous visitor,
which is the whole point: a trace nobody can open is not a debugging link.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from tests.e2e.conftest import parse_sse, post_ask_body

pytestmark = pytest.mark.e2e

LIVE_LANGFUSE = os.environ.get("CADRE_E2E_LANGFUSE") == "1"
requires_langfuse = pytest.mark.skipif(
    not LIVE_LANGFUSE,
    reason=(
        "live-tracing e2e is opt-in: set CADRE_E2E_LANGFUSE=1 against a target "
        "whose LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_HOST are real. "
        "Tracing fails open, so without the gate a Langfuse outage would pass "
        "silently instead of failing (KB-009)."
    ),
)


def _ask_body(message: str) -> tuple[str, dict[str, str]]:
    return post_ask_body(
        {"conversation_id": uuid.uuid4().hex[:16], "message": message}
    )


@requires_langfuse
class TestTraceLink:
    def test_the_trace_event_is_the_first_frame_and_advertises_a_url(self, http):
        raw, headers = _ask_body("What does Cadre AI do?")
        response = http.post("/ask", content=raw, headers=headers)
        events = parse_sse(response.text)

        assert events, "the target returned no SSE frames at all"
        assert events[0][0] == "trace", (
            f"first frame was {events[0][0]!r} — tracing is down on this target "
            f"(check LANGFUSE_* in its environment), or the event moved"
        )
        payload = events[0][1]
        assert set(payload) == {"trace_id", "url"}
        assert payload["trace_id"]
        assert payload["url"].startswith("http")
        assert events[-1][0] == "done"

    def test_the_advertised_trace_resolves_for_an_anonymous_visitor(self, http):
        # Proves both halves at once: the trace was really created in Langfuse
        # *and* it was marked public. A trace that exists but 404s or bounces a
        # logged-out visitor is a link that helps nobody.
        raw, headers = _ask_body("What does Cadre AI do?")
        events = parse_sse(http.post("/ask", content=raw, headers=headers).text)
        url = events[0][1]["url"]

        # Langfuse ingests asynchronously; the flush happened before `done`, but
        # the read side can lag a few seconds behind it.
        last = None
        for _ in range(20):
            last = httpx.get(url, follow_redirects=True, timeout=30.0)
            if last.status_code < 400:
                break
            import time

            time.sleep(3)

        assert last is not None and last.status_code < 400, (
            f"trace url {url} answered {last.status_code if last else 'nothing'}"
        )

    def test_the_traced_turn_still_streams_unbuffered(self, http):
        # KB-010: the cheapest CI-checkable buffering detector, reused rather
        # than reinvented — the `trace` event adds a frame to the same stream,
        # so it is another chance for something in front to start buffering.
        raw, headers = _ask_body("What does Cadre AI do?")
        with http.stream("POST", "/ask", content=raw, headers=headers) as response:
            assert "content-length" not in response.headers
            lines = list(response.iter_lines())

        assert lines[0].startswith("event: trace"), (
            f"first line on the wire was {lines[0]!r}"
        )
