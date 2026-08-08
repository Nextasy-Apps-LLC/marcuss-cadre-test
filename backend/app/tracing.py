"""Langfuse traceability — the whole tracing surface, in one module.

Same single-purpose-module shape as `app/llm.py` and `app/ratelimit.py`: the
graph and the request path know two function names and nothing about Langfuse.

Three properties are load-bearing:

* **Credentials are read once, at import.** Never per request. An SSM lookup or
  a credential round trip inside a turn spends part of CloudFront's 60s cap
  (KB-004) on something that cannot change between requests. Terraform puts the
  values on the function as `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` /
  `LANGFUSE_HOST` (see `infra/langfuse.tf`); `docker run -e` does the same
  locally.
* **Everything here is fail-open.** A Langfuse outage degrades observability,
  never a visitor's turn — the same posture as a degraded step verdict. Both
  public functions swallow every exception.
* **Fail-open stays visible (KB-009).** A degrade that looks identical to a
  healthy trace is the bug fail-open invites, so every disabled or failed path
  logs, and the client is told by the *absence* of a `trace` event rather than
  by a broken one.

What the trace contains is fixed by `backend/CLAUDE.md`: the `client_id` as the
Langfuse session id (so a visitor's turns group), which step refused if any,
and per-step + total `latency_ms`. The per-step numbers are the `elapsed_ms`
values already on the SSE wire, handed down from `app/main.py` — not a second
timing mechanism that could disagree with the stepper.

The handler this module hands out is Langfuse's LangChain `CallbackHandler`,
which rides the graph invocation's `config` (KB-008), so it captures
**graph-level node spans**. It does not capture individual Bedrock calls as
generations: since ADR 0002 the model path is plain `httpx` with no LangChain
in it. That is the accepted shape, not an omission — see the issue #53 spec.
"""

from __future__ import annotations

import logging
import os

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langfuse.types import TraceContext

log = logging.getLogger("cadre.tracing")

# Name of the span `finalize_trace` writes the trace-level fields on. It is not
# the turn's only span — the graph's nodes arrive as their own spans from the
# callback handler — it is the one that carries session/refusal/latency.
TURN_SPAN_NAME = "turn"

# What `refused_step` says when the turn was not refused.
#
# Not cosmetic: Langfuse drops metadata keys whose value is null, so passing
# `None` here does not record "no refusal" — it records nothing at all, and the
# trace becomes unable to distinguish a clean turn from one where the tracing
# code failed to set the field. That ambiguity is the exact shape of KB-009.
# A literal is also the only version you can filter on in the Langfuse UI,
# which is the whole reason the field exists.
NOT_REFUSED = "none"

# Ceiling on any single Langfuse HTTP call. Tracing is not allowed to spend a
# meaningful slice of the 60s turn budget (KB-004) waiting on an outage; a
# timeout here becomes a dropped trace, which is the correct trade.
REQUEST_TIMEOUT_S = 5

# A syntactically valid trace id used once at import to resolve (and cache) the
# project id `get_trace_url` needs. Doing it here keeps that HTTP round trip out
# of every turn, and turns a bad key into a container-start warning instead of a
# per-request surprise. It never becomes a real trace — nothing is written.
_PROBE_TRACE_ID = "0" * 32

_client: Langfuse | None = None
_ENABLED = False


def _configure(*, public_key: str, secret_key: str, host: str) -> bool:
    """Build the client singleton. Returns whether tracing came up.

    Called once at import. Kept as a function taking explicit arguments rather
    than reading the globals directly so the disabled paths are testable without
    reaching into the environment.
    """
    global _client, _ENABLED

    missing = [
        name
        for name, value in (
            ("LANGFUSE_PUBLIC_KEY", public_key),
            ("LANGFUSE_SECRET_KEY", secret_key),
            ("LANGFUSE_HOST", host),
        )
        if not value
    ]
    if missing:
        log.warning(
            "tracing disabled: %s not set — turns will answer normally but carry "
            "no trace and no trace link",
            ", ".join(missing),
        )
        return False

    try:
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            timeout=REQUEST_TIMEOUT_S,
        )
        # Also the credential check: this is the first call that actually talks
        # to Langfuse, so a wrong key fails here, loudly, at container start.
        if client.get_trace_url(trace_id=_PROBE_TRACE_ID) is None:
            log.warning(
                "tracing disabled: Langfuse at %s returned no project for these "
                "credentials — turns will answer without a trace",
                host,
            )
            return False
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning(
            "tracing disabled: could not reach Langfuse at %s (%s) — turns will "
            "answer without a trace",
            host,
            exc,
        )
        return False

    _client, _ENABLED = client, True
    log.info("tracing enabled against %s", host)
    return True


def start_trace(
    client_id: str,
) -> tuple[CallbackHandler | None, str | None, str | None]:
    """Open a trace for one turn: `(handler, trace_id, url)`.

    The id is generated locally, which is what lets `/ask` put the trace URL on
    the wire as the very first frame — before the graph has run, let alone
    finished. Returns `(None, None, None)` when tracing is down; never raises.
    """
    if not _ENABLED or _client is None:
        return None, None, None

    try:
        trace_id = Langfuse.create_trace_id()
        handler = CallbackHandler(trace_context=TraceContext(trace_id=trace_id))
        # The SDK's own helper: it knows the project id, which the public trace
        # path contains and we would otherwise have to guess at.
        url = _client.get_trace_url(trace_id=trace_id)
        if not url:
            log.warning("trace url unavailable for %s — emitting no trace event", trace_id)
            return None, None, None
        return handler, trace_id, url
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning("could not start a trace (turn is unaffected): %s", exc)
        return None, None, None


def finalize_trace(
    trace_id: str | None,
    refused_step: str | None,
    step_latencies: dict[str, int],
    total_latency_ms: int,
    client_id: str,
) -> None:
    """Write the trace-level fields and flush. Must run before the terminal SSE
    event: Langfuse batches in a background thread and Lambda freezes the
    instance the moment the response ends, so an unflushed batch is a trace that
    silently never arrives.

    No-op when `trace_id` is `None` (tracing was down at `start_trace`) and on
    any exception — a dropped trace is never allowed to become a dropped turn.
    """
    if not _ENABLED or _client is None or trace_id is None:
        return

    try:
        # Order matters, and not for a tidy-code reason. The graph's spans and
        # this one both carry trace-level attributes; when they land in the same
        # export batch the LangChain root span wins the trace upsert and
        # `public`/`session_id` are silently dropped — verified against Langfuse
        # Cloud, where the trace came back `public: false` every time. Flushing
        # the graph's spans first makes this span's fields the later write.
        _client.flush()

        with propagate_attributes(session_id=client_id):
            with _client.start_as_current_observation(
                name=TURN_SPAN_NAME,
                trace_context=TraceContext(trace_id=trace_id),
                metadata={
                    "refused_step": refused_step or NOT_REFUSED,
                    "latency_ms": dict(step_latencies),
                    "total_latency_ms": total_latency_ms,
                },
            ) as span:
                # Public traces are the point: the URL on the wire has to open
                # for a visitor who has never seen this Langfuse project.
                span.set_trace_as_public()

        _client.flush()
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning("could not finalize trace %s (turn is unaffected): %s", trace_id, exc)


_configure(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "").strip(),
    host=os.environ.get("LANGFUSE_HOST", "").strip(),
)
