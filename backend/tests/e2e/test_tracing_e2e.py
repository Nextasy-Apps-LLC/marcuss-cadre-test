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
reads the wire and then asks Langfuse, **anonymously**, whether the advertised
trace exists and is public. That is the whole point: a trace nobody can open is
not a debugging link.

## Why the public-visibility check does not fetch the trace page

The obvious check — `GET` the advertised
`{host}/project/{project}/traces/{trace}` URL and assert it is not an error —
cannot fail, and shipped for one review cycle doing exactly that. That path is
a Next.js SPA shell: it answers `200` with the same 6824-byte document for a
public trace, for a trace that exists but was never marked public, and for a
trace id that was never created at all (measured on 2026-08-08 against
Langfuse Cloud: the three responses were byte-identical). The trace itself is
fetched by the SPA *afterwards*, from the tRPC endpoint below — so asserting on
the shell asserts that Langfuse serves a web app, not that this turn was
traced. That is the KB-009 silent degrade this file's gate exists to prevent,
reintroduced inside the file's own assertions.

`traces.byId` is the endpoint the SPA itself calls, it needs no credentials,
and it distinguishes all three states (measured, same session):

| state                                   | response                                        |
| --------------------------------------- | ----------------------------------------------- |
| created + finalized + marked public      | `200`, body `"public": true`                     |
| created but never finalized (not public) | `401 "…this trace is not public"`                |
| never created                            | `404 "Trace not found"`                          |

`TestThePublicVisibilityProbeCanFail` keeps that discriminating power honest:
if Langfuse ever starts answering `200` for an unknown id, the probe stops
proving anything and that case fails rather than the suite going quietly green.
"""

from __future__ import annotations

import json
import os
import re
import time
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

# The shape `app/tracing.py` puts on the wire, via the SDK's `get_trace_url()`:
# `{host}/project/{project_id}/traces/{trace_id}`. Parsed rather than assumed so
# the anonymous read-back below can address the same trace in the same project.
TRACE_URL = re.compile(
    r"^(?P<host>https?://[^/]+)/project/(?P<project>[^/]+)/traces/(?P<trace>[^/?#]+)$"
)

# Langfuse Cloud ingests asynchronously — the flush completes before `done`, but
# the read side lags well behind it (measured 2026-08-08: ~30-60s from `done` to
# the trace answering on `traces.byId`; Langfuse's own docs warn about minutes
# under load). This budget is generous on purpose: a *slow* trace and a *missing*
# trace must not look the same to this suite.
READBACK_BUDGET_S = 180
READBACK_INTERVAL_S = 6


def _ask_body(message: str, conversation_id: str) -> tuple[str, dict[str, str]]:
    return post_ask_body({"conversation_id": conversation_id, "message": message})


def _read_trace_anonymously(host: str, project: str, trace_id: str) -> httpx.Response:
    """Ask Langfuse for one trace with no credentials at all.

    No auth header, no cookies, no `LANGFUSE_*` from this process's environment —
    an anonymous visitor holding nothing but the link off the wire.
    """
    return httpx.get(
        f"{host}/api/trpc/traces.byId",
        params={
            "input": json.dumps({"json": {"traceId": trace_id, "projectId": project}})
        },
        timeout=30.0,
    )


def _langfuse_error(response: httpx.Response) -> str:
    """Langfuse's own words for why a read failed — the thing worth reporting."""
    try:
        return response.json()["error"]["json"]["message"]
    except Exception:  # noqa: BLE001 - a failure message must never itself raise
        return response.text[:200]


@pytest.fixture(scope="module")
def traced_turn(http) -> dict:
    """One real, complete turn, shared by the cases that only need its trace.

    Module-scoped because each turn is a real (paid) model round trip and the
    read-back cases assert on the same trace from different angles.
    """
    conversation_id = uuid.uuid4().hex[:16]
    raw, headers = _ask_body("What does Cadre AI do?", conversation_id)
    events = parse_sse(http.post("/ask", content=raw, headers=headers).text)

    assert events, "the target returned no SSE frames at all"
    assert events[0][0] == "trace", (
        f"first frame was {events[0][0]!r} — tracing is down on this target "
        f"(check LANGFUSE_* in its environment), or the event moved"
    )
    assert events[-1][0] == "done", f"turn did not reach done: {events[-1][0]!r}"

    match = TRACE_URL.match(events[0][1]["url"])
    assert match, f"trace url {events[0][1]['url']!r} is not a project trace url"
    return {
        "conversation_id": conversation_id,
        "payload": events[0][1],
        "host": match["host"],
        "project": match["project"],
        "trace_id": match["trace"],
        "done_at": time.monotonic(),
    }


@requires_langfuse
class TestTraceLink:
    def test_the_trace_event_is_the_first_frame_and_advertises_a_url(self, http):
        conversation_id = uuid.uuid4().hex[:16]
        raw, headers = _ask_body("What does Cadre AI do?", conversation_id)
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
        assert TRACE_URL.match(payload["url"]), (
            f"advertised url {payload['url']!r} is not a Langfuse project trace url"
        )
        assert TRACE_URL.match(payload["url"])["trace"] == payload["trace_id"], (
            "the advertised url points at a different trace than the advertised id"
        )
        assert events[-1][0] == "done"

    def test_the_advertised_trace_exists_in_langfuse_and_is_public(
        self, traced_turn
    ):
        # Both halves at once, and neither of them provable from the trace page:
        # the trace was really *created* in Langfuse (not 404), and it was really
        # marked *public* (not 401), and it is the trace this turn advertised
        # (matching id and session). See the module docstring for why this asks
        # the API the SPA asks rather than fetching the SPA.
        deadline = traced_turn["done_at"] + READBACK_BUDGET_S
        while True:
            response = _read_trace_anonymously(
                traced_turn["host"], traced_turn["project"], traced_turn["trace_id"]
            )
            if response.status_code == 200 or time.monotonic() >= deadline:
                break
            time.sleep(READBACK_INTERVAL_S)

        assert response.status_code == 200, (
            f"anonymous read of trace {traced_turn['trace_id']} answered "
            f"{response.status_code}: {_langfuse_error(response)!r}. "
            f"404 means the trace never arrived (finalize_trace's flush did not "
            f"beat the terminal frame, or the ingest was rejected); 401 means it "
            f"arrived but was never marked public, so the link on the wire opens "
            f"nothing for a visitor. Waited {READBACK_BUDGET_S}s for ingestion."
        )

        trace = response.json()["result"]["data"]["json"]
        assert trace["public"] is True, (
            f"trace {traced_turn['trace_id']} is readable but public={trace['public']!r}"
        )
        assert trace["id"] == traced_turn["trace_id"]
        assert trace["sessionId"] == traced_turn["conversation_id"], (
            "the trace is not grouped under this turn's conversation_id — "
            f"{trace['sessionId']!r} != {traced_turn['conversation_id']!r}"
        )

    def test_the_traced_turn_still_streams_unbuffered(self, http):
        # KB-010: the cheapest CI-checkable buffering detector, reused rather
        # than reinvented — the `trace` event adds a frame to the same stream,
        # so it is another chance for something in front to start buffering.
        conversation_id = uuid.uuid4().hex[:16]
        raw, headers = _ask_body("What does Cadre AI do?", conversation_id)
        with http.stream("POST", "/ask", content=raw, headers=headers) as response:
            assert "content-length" not in response.headers
            lines = list(response.iter_lines())

        assert lines[0].startswith("event: trace"), (
            f"first line on the wire was {lines[0]!r}"
        )


@requires_langfuse
class TestThePublicVisibilityProbeCanFail:
    """Guards the check above against becoming unfalsifiable again.

    An assertion that cannot fail is worse than no assertion: it reports success
    for a broken target. The previous version of this file asserted `HTTP < 400`
    on the trace *page*, which answers `200` for a trace that was never created —
    so it passed for a target that had silently stopped tracing. This case runs
    the probe against a state that is known to be wrong; the moment Langfuse
    answers `200` for a trace id nobody ever wrote, the check above has stopped
    discriminating and this suite says so rather than going green on nothing.
    """

    def test_a_trace_id_that_was_never_created_is_not_readable(self, traced_turn):
        never_created = uuid.uuid4().hex
        response = _read_trace_anonymously(
            traced_turn["host"], traced_turn["project"], never_created
        )

        assert response.status_code != 200, (
            f"Langfuse answered 200 for trace id {never_created}, which was never "
            f"created — the public-visibility check above can no longer tell a "
            f"traced turn from an untraced one"
        )
        assert response.status_code == 404, (
            f"expected 404 for an unknown trace id, got {response.status_code}: "
            f"{_langfuse_error(response)!r}"
        )
