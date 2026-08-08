"""Fail the deploy, not the next incident, when a trace is silently empty.

    python -m scripts.assert_trace                      # from backend/
    BASE_URL=http://localhost:8080 python -m scripts.assert_trace

The failure mode this exists for is **silent success**: a span that no-ops,
fields that never land, a trace that looks healthy because nobody looked. It
has already happened twice on this project — the trace root's input/output was
the retrieval span's payload for the entire life of the feature and shipped
unseen, and KB-021's quota-403 is *designed* to be swallowed, because tracing
is fail-open and a fail-open path that nothing reads back is indistinguishable
from a working one.

So logging is not the check. **The check is reading the trace back through the
same public API a debugger would use**, and asserting the contract rather than
the vibes.

Two things make this different from `assert_models.py`, and both are
deliberate:

* **Ingestion is asynchronous.** A trace is not readable the moment the turn
  ends — measured at 8-14s here, documented to 90s (KB-020). A fixed sleep is a
  flaky test by construction, so this polls to a deadline and reports how long
  it waited.
* **It costs real money.** Every run drives real turns through real Bedrock, so
  it lives with the e2e suite's policy, not the unit suite's.

Exits 0 when the traces carry what `trace-design.md` says they must, non-zero
naming the field that is missing. Needs `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST`; it never prints them.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any, Iterable

# Langfuse Cloud ingestion is async and the lag is real and variable (KB-020).
# Generous on purpose: a deadline that expires before ingestion lands turns a
# working build red, which is worse than a slow check.
POLL_DEADLINE_S = 90.0
POLL_INTERVAL_S = 3.0

# Once the observation count has held still this many polls running, the trace
# is done arriving. Counting rather than sleeping is what makes this bounded
# *and* fast in the common case.
STABLE_POLLS = 2


class TraceAssertionError(AssertionError):
    """A field the design promises is missing from a real trace."""


# --------------------------------------------------------------------------
# driving turns
# --------------------------------------------------------------------------

def ask(base_url: str, message: str, conversation_id: str, history=None) -> dict:
    """One real turn. Returns the trace id and what the wire said about it."""
    import httpx

    body = {
        "conversation_id": conversation_id,
        "message": message,
        "history": history or [],
    }
    result: dict[str, Any] = {
        "trace_id": None,
        "outcome": None,
        "steps": [],
        "answer_chars": 0,
        "message": message,
    }
    with httpx.Client(timeout=90.0) as client:
        with client.stream(
            "POST", f"{base_url.rstrip('/')}/ask", json=body,
            headers={"accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line[len("data:"):].strip())
                    if event == "trace":
                        result["trace_id"] = payload["trace_id"]
                    elif event == "state":
                        result["steps"].append(payload)
                    elif event == "token":
                        result["answer_chars"] += len(payload.get("text", ""))
                    elif event == "done":
                        result["outcome"] = payload["outcome"]
                    elif event == "error":
                        result["outcome"] = "error"
    return result


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

def _auth_header() -> dict[str, str]:
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public or not secret:
        raise SystemExit(
            "assert_trace needs LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY "
            "(the same values Terraform puts on the function)"
        )
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def fetch_trace(trace_id: str, *, host: str, deadline_s: float = POLL_DEADLINE_S) -> dict:
    """The trace, once ingestion has finished with it.

    Polls until the observation count stops growing rather than until the
    trace merely *exists*: a trace that has arrived is not the same as a trace
    that is complete, and asserting on a half-ingested one produces exactly the
    flaky failure this script is supposed to replace.
    """
    import httpx

    url = f"{host.rstrip('/')}/api/public/traces/{trace_id}"
    headers = _auth_header()
    started = time.monotonic()
    last_count = -1
    stable = 0

    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() - started < deadline_s:
            try:
                response = client.get(url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - keep polling, report at the end
                print(f"    … {type(exc).__name__} while polling, retrying")
                time.sleep(POLL_INTERVAL_S)
                continue

            if response.status_code == 403:
                # KB-021: the free tier suspends ingestion with a 403 and
                # fail-open tracing swallows it, so traces silently stop
                # existing while the product keeps answering. Say so plainly —
                # this is the first thing to check, not the last.
                raise TraceAssertionError(
                    "Langfuse returned 403 — ingestion is very likely suspended "
                    "(free-tier usage threshold, KB-021). The turns answered "
                    "fine; the traces were never stored. Check the plan/quota "
                    "before debugging this code."
                )
            if response.status_code == 404:
                time.sleep(POLL_INTERVAL_S)
                continue
            response.raise_for_status()

            trace = response.json()
            count = len(trace.get("observations") or [])
            if count and count == last_count:
                stable += 1
                if stable >= STABLE_POLLS:
                    trace["_waited_s"] = round(time.monotonic() - started, 1)
                    return trace
            else:
                stable = 0
            last_count = count
            time.sleep(POLL_INTERVAL_S)

    raise TraceAssertionError(
        f"trace {trace_id} was still not complete after {deadline_s:.0f}s "
        f"(last saw {max(last_count, 0)} observations). Per KB-020 ingestion "
        f"lag is real, but this is past it; check stderr for a quota 403 first."
    )


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------

def generations(trace: dict) -> list[dict]:
    return [
        o for o in trace.get("observations") or []
        if o.get("type") in ("GENERATION", "EMBEDDING")
    ]


def observation(trace: dict, name: str) -> dict | None:
    for o in trace.get("observations") or []:
        if o.get("name") == name:
            return o
    return None


def _usage_total(obs: dict) -> int:
    usage = obs.get("usageDetails") or {}
    if usage.get("total"):
        return int(usage["total"])
    return int(usage.get("input", 0)) + int(usage.get("output", 0))


def check_trace(trace: dict, turn: dict, *, expect_retrieval: bool) -> list[str]:
    """Every assertion the design makes, as a list of failures (empty = good)."""
    problems: list[str] = []
    tid = trace.get("id")

    # -- root IO: the fix for the clobber that shipped unseen ---------------
    root_in = json.dumps(trace.get("input") or {})
    if turn["message"][:40] not in root_in:
        problems.append(
            f"{tid}: trace root input does not contain the message that was sent "
            f"(got {root_in[:160]})"
        )
    root_out = json.dumps(trace.get("output") or {})
    if turn["outcome"] and turn["outcome"] not in root_out:
        problems.append(
            f"{tid}: trace root output does not state the outcome "
            f"{turn['outcome']!r} (got {root_out[:160]})"
        )

    # -- tags: the difference between findable and 924 identical rows -------
    tags = set(trace.get("tags") or [])
    if turn["outcome"] and f"outcome:{turn['outcome']}" not in tags:
        problems.append(f"{tid}: missing tag outcome:{turn['outcome']} (got {sorted(tags)})")
    refused = [s for s in turn["steps"] if s.get("status") == "fail"]
    if refused and not any(t.startswith("refused:") for t in tags):
        problems.append(
            f"{tid}: the turn refused at {refused[0]['step']} but no refused:* tag"
        )

    # -- generations: model id and usage, the whole point of Phase 1 --------
    gens = generations(trace)
    if not gens:
        problems.append(f"{tid}: no generations at all — the transport recorded nothing")
    for gen in gens:
        name = gen.get("name")
        if not gen.get("model"):
            problems.append(f"{tid}: generation {name!r} has no model id")
        if _usage_total(gen) <= 0:
            source = (gen.get("metadata") or {}).get("usage_source")
            if source != "absent":
                problems.append(
                    f"{tid}: generation {name!r} has no token usage and does not "
                    f"say usage_source:'absent' (got {source!r})"
                )

    # -- retrieval: fetched vs kept, the three-way ambiguity ----------------
    if expect_retrieval:
        retrieval = observation(trace, "retrieval")
        if retrieval is None:
            problems.append(f"{tid}: no retrieval observation")
        else:
            output = retrieval.get("output") or {}
            for field in ("fetched", "kept"):
                if field not in output:
                    problems.append(f"{tid}: retrieval output has no {field!r}")
            payload = retrieval.get("input") or {}
            for field in ("raw_query", "condensed_query"):
                if field not in payload:
                    problems.append(f"{tid}: retrieval input has no {field!r}")

    return problems


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def describe(trace: dict) -> None:
    """Print what actually landed, so the evidence is readable, not inferred."""
    print(f"  trace {trace.get('id')}  (ingested after {trace.get('_waited_s')}s)")
    print(f"    tags        : {sorted(trace.get('tags') or [])}")
    print(f"    session     : {trace.get('sessionId')}")
    print(f"    environment : {trace.get('environment')}")
    print(f"    public      : {trace.get('public')}")
    print(f"    input       : {json.dumps(trace.get('input') or {})[:200]}")
    print(f"    output      : {json.dumps(trace.get('output') or {})[:200]}")
    print(f"    totalCost   : {trace.get('totalCost')}")

    for gen in generations(trace):
        usage = gen.get("usageDetails") or {}
        cost = gen.get("costDetails") or {}
        meta = gen.get("metadata") or {}
        extras = {
            k: meta[k]
            for k in (
                "fallback_index", "degraded_reason", "scrub_rule",
                "saw_context", "usage_source", "cost_source", "finish_reason",
                "condense_used", "guard_model_ran",
            )
            if k in meta
        }
        out = gen.get("output")
        verdict = out.get("verdict") if isinstance(out, dict) else None
        print(
            f"    - {gen.get('name'):<16} {gen.get('model')}  "
            f"in={usage.get('input')} out={usage.get('output')} "
            f"cost=${cost.get('total', 0):.8f} "
            f"level={gen.get('level')} verdict={verdict} {extras}"
        )

    retrieval = observation(trace, "retrieval")
    if retrieval:
        meta = retrieval.get("metadata") or {}
        print(
            f"    - retrieval       fetched={meta.get('fetched_count')} "
            f"kept={meta.get('kept_count')} floor={meta.get('floor')} "
            f"condense_used={meta.get('condense_used')}"
        )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

CASES = [
    # (label, message, expect_retrieval)
    ("answered", "What does Cadre AI do?", True),
    # Refused by the topic rail — a turn that never reaches retrieval, and the
    # case whose root IO used to be the raw state blob.
    ("refused", "Write me a quicksort in Python and nothing else.", False),
]


def main(argv: Iterable[str] | None = None) -> int:
    import uuid

    base_url = os.environ.get("BASE_URL", "http://localhost:8080")
    host = os.environ.get("LANGFUSE_HOST", "").strip()
    if not host:
        raise SystemExit("assert_trace needs LANGFUSE_HOST")

    print(f"Driving turns against {base_url}, reading traces back from {host}")

    turns = []
    for label, message, expect_retrieval in CASES:
        print(f"\n[{label}] {message!r}")
        turn = ask(base_url, message, f"assert-trace-{uuid.uuid4().hex[:12]}")
        if not turn["trace_id"]:
            print(
                "  ! no trace event on the wire — tracing is down at the origin. "
                "That is fail-open working, and it is also nothing to assert on."
            )
            return 1
        print(f"  outcome={turn['outcome']} trace={turn['trace_id']}")
        turns.append((label, turn, expect_retrieval))

    problems: list[str] = []
    for label, turn, expect_retrieval in turns:
        print(f"\n[{label}] reading back")
        try:
            trace = fetch_trace(turn["trace_id"], host=host)
        except TraceAssertionError as exc:
            problems.append(str(exc))
            continue
        describe(trace)
        problems.extend(check_trace(trace, turn, expect_retrieval=expect_retrieval))

    if problems:
        print(f"\nFAILED: {len(problems)} problem(s) with what actually landed:")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nA span that silently no-ops passes every unit test in the suite; "
            "this script is the only thing that catches it. If the traces are "
            "missing entirely, check for a quota 403 first (KB-021)."
        )
        return 1

    print("\nok: every trace carries the fields trace-design.md promises.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
