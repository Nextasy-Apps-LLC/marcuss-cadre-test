"""Langfuse traceability — the whole tracing surface, in one module.

Same single-purpose-module shape as `app/llm.py` and `app/ratelimit.py`: the
graph and the request path know a handful of function names and nothing about
Langfuse.

Three properties are load-bearing:

* **Credentials are read once, at import.** Never per request. An SSM lookup or
  a credential round trip inside a turn spends part of CloudFront's 60s cap
  (KB-004) on something that cannot change between requests. Terraform puts the
  values on the function as `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` /
  `LANGFUSE_HOST` (see `infra/langfuse.tf`); `docker run -e` does the same
  locally.
* **Everything here is fail-open.** A Langfuse outage degrades observability,
  never a visitor's turn — the same posture as a degraded step verdict. Every
  public function and every method on the handles below swallows every
  exception. A dropped span must never become a dropped turn.
* **Fail-open stays visible (KB-009).** A degrade that looks identical to a
  healthy trace is the bug fail-open invites, so every disabled or failed path
  logs, and the client is told by the *absence* of a `trace` event rather than
  by a broken one. The same rule drives the *literals* in this module: Langfuse
  drops metadata keys whose value is null, so `NOT_REFUSED`, `USAGE_ABSENT` and
  `COST_UNPRICED` exist because "field missing" and "instrumentation never ran"
  must not look the same.

What the trace contains is fixed by `trace-design.md` (issue #75) and its
Phase 1 implementation, issue #79: the `client_id` as the Langfuse session id
(so a visitor's turns group), which step refused if any, per-step + total
`latency_ms`, the turn's own input/output on the trace root, outcome/refusal/
degraded/kb tags, and **one hand-built generation per model call** carrying the
effective model id, the provider's own token usage and the cost computed from
`config.MODEL_PRICES`. The per-step latencies are the `elapsed_ms` values
already on the SSE wire, handed down from `app/main.py` — not a second timing
mechanism that could disagree with the stepper.

**On instrumenting the transport.** `backend/CLAUDE.md` used to say "don't
instrument `app/llm.py` to work around it", where "it" was the LangChain
`CallbackHandler` capturing only graph-level node spans. `trace-design.md` §4.2
supersedes that sentence deliberately: the rule guarded against bespoke
per-call-site logging duplicating the callback surface, but the model id and
the token counts exist **only** inside the HTTP response that `llm.chat`,
`llm.chat_stream` and `embeddings.embed_query` parse and discard. No other
layer can ever know those numbers. So the transport grows exactly one seam — an
optional `generation=` handle it reports into — and nothing else.

**On ambient context.** This module used to say it "never looks at ambient
context", to stop one visitor's spans attaching to another's trace. Phase 1
supersedes the letter of that line while keeping its reason: the turn span is
opened *inside the graph task* (`main._run_graph`), so the contextvar carrying
it is task-local, and task-local is precisely the isolation the line wanted
(KB-008). Generations created anywhere inside that task parent themselves with
**zero ids threaded** — which is what keeps `models.py`'s seams
monkeypatchable in one line. `tests/test_tracing_phase1.py` runs two
interleaved turns and asserts no observation crosses traces; verify, don't
trust.

The handler this module still hands out is Langfuse's LangChain
`CallbackHandler`, which rides the graph invocation's `config` (KB-008) and
captures graph-level node spans. `trace-design.md` §6 proposes replacing it
with `_record`-driven step spans — that is Phase 2, deliberately not this
issue: Phase 1 must not gut the only tree while the new observations are still
proving themselves.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from typing import Any, Iterator

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langfuse.types import TraceContext

from app import config

log = logging.getLogger("cadre.tracing")

# Name of the span `finalize_trace` writes the trace-level fields on: session,
# tags, public, refusal, latencies and the trace's own input/output.
TURN_SPAN_NAME = "turn"

# Name of the *structural* span opened inside the graph task, which every
# generation parents itself to.
#
# These are two spans on purpose, and the reason is empirical. The obvious
# design is one span that both parents the generations and carries the
# trace-level fields — `trace-design.md` §4.9 assumes exactly that. It does not
# work, and readback is how we know:
#
#  * `propagate_attributes(session_id=…, tags=…)` applies to spans created
#    *inside* its block. The structural span has to be created in the graph
#    task, long before the turn's outcome (and therefore its tags) is known, so
#    it can never be inside that block — a single span silently loses
#    `session_id` and every tag.
#  * The trace-level upsert is won by the *later-created* root-level write, not
#    the later-ended one. A span opened before the graph runs loses to the
#    LangChain root span no matter when it ends.
#
# Both were observed on real traces during issue #79 (session `None`, `tags: []`,
# and the trace root still showing the state blob) and both disappear when the
# fields are written by a span created after the first flush, which is what
# `finalize_trace` has always done.
PIPELINE_SPAN_NAME = "pipeline"

# Name of the span `record_retrieval` writes. Separate from the graph's own
# `retrieve` node span, which the callback handler produces and which knows
# only that the node ran and how long it took — not what it searched for.
RETRIEVAL_SPAN_NAME = "retrieval"

# Name of the embedding observation `embeddings.embed_query` writes.
EMBEDDING_OBSERVATION_NAME = "embedding"

# What `refused_step` says when the turn was not refused.
#
# Not cosmetic: Langfuse drops metadata keys whose value is null, so passing
# `None` here does not record "no refusal" — it records nothing at all, and the
# trace becomes unable to distinguish a clean turn from one where the tracing
# code failed to set the field. That ambiguity is the exact shape of KB-009.
# A literal is also the only version you can filter on in the Langfuse UI,
# which is the whole reason the field exists.
NOT_REFUSED = "none"

# The same rule, twice more. A generation whose provider returned no `usage`
# says so; a model with no price line says so. Silence would read as "the
# instrumentation is broken", which is the thing this module must never be
# ambiguous about.
USAGE_ABSENT = "absent"
USAGE_PRESENT = "provider"
COST_UNPRICED = "unpriced"
COST_COMPUTED = "model_prices"

# `_judge` and the guard can return a lot of text (a reasoning model monologues
# before answering, KB-011). The raw output is on the trace so `_label`'s
# last-match-wins parse is auditable, but the whole monologue is quota
# (KB-021 — payload volume is quota, and quota exhaustion here is invisible by
# design), and 500 characters is enough to see what the model concluded.
MAX_RAW_CHARS = 500

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

# The turn whose accumulator `record_response` folds usage and cost into.
# Task-local, set and cleared by `Turn.activate()` alongside the span, so the
# per-step numbers land in the right turn without threading an id — the same
# isolation story as the ambient span (KB-008), and asserted by the
# interleaved-turns test.
_current_turn: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
    "_current_turn", default=None
)


def _configure(
    *, public_key: str, secret_key: str, host: str, environment: str = "dev"
) -> bool:
    """Build the client singleton. Returns whether tracing came up.

    Called once at import. Kept as a function taking explicit arguments rather
    than reading the globals directly so the disabled paths are testable without
    reaching into the environment.

    `environment` separates prod from a laptop in every Langfuse aggregate —
    without it a developer's turns silently pollute every cost and latency
    number the dashboards show.
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
            environment=environment,
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
    log.info("tracing enabled against %s (environment=%s)", host, environment)
    return True


# --------------------------------------------------------------------------
# usage and cost
# --------------------------------------------------------------------------

def _usage_details(usage: Any) -> dict[str, int] | None:
    """The provider's `usage` object as Langfuse's `{input, output, total}`.

    Reused, never re-measured (design principle 5): these are the numbers the
    provider will bill, so a local tokenizer here would be a second opinion
    that can only ever be wrong. Mantle returns OpenAI's
    `prompt_tokens`/`completion_tokens`; the `input_tokens`/`output_tokens`
    spelling is accepted too, because it costs one line and a provider that
    changes spelling would otherwise silently zero every cost.
    """
    if not isinstance(usage, dict):
        return None

    def _first(*names: str) -> int | None:
        for name in names:
            value = usage.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    prompt = _first("prompt_tokens", "input_tokens")
    completion = _first("completion_tokens", "output_tokens")
    total = _first("total_tokens")

    if prompt is None and completion is None and total is None:
        return None

    prompt = prompt or 0
    completion = completion or 0
    if total is None:
        total = prompt + completion
    return {"input": prompt, "output": completion, "total": total}


def _cost_details(model_id: str | None, usage: dict[str, int]) -> dict[str, float] | None:
    """USD for this call, from the in-repo price table.

    Computed here rather than configured as a Langfuse model definition because
    every model id is env-overridable (`CADRE_MODEL_*`): a UI-side pricing table
    matches against ids that can change without a deploy and drifts silently,
    invisible in any diff. `config.MODEL_PRICES` is reviewed like everything
    else, and one unit test asserts every configured id has a line
    (`trace-design.md` §4.7).
    """
    price = config.MODEL_PRICES.get(model_id or "")
    if not price:
        return None
    input_per_m, output_per_m = price
    input_cost = usage.get("input", 0) * input_per_m / 1_000_000
    output_cost = usage.get("output", 0) * output_per_m / 1_000_000
    return {
        "input": input_cost,
        "output": output_cost,
        "total": input_cost + output_cost,
    }


def truncate(text: str | None) -> str:
    """A model's raw answer, bounded. See `MAX_RAW_CHARS`."""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= MAX_RAW_CHARS else text[:MAX_RAW_CHARS]


# --------------------------------------------------------------------------
# generations
# --------------------------------------------------------------------------

class Generation:
    """One model call on the trace, fail-open at every method.

    Constructed with `observation=None` it is a working no-op, which is what
    lets `llm.py` and `models.py` call these methods unconditionally instead of
    branching on whether tracing is up at every call site.

    The split of responsibilities is deliberate. The **transport** calls
    `record_response()`, because the effective model id and the token usage
    exist only inside the HTTP response it parses. The **caller** (`models.py`)
    calls `finish()`, because the verdict exists only after `_label()` has read
    that response. Neither knows what the other knows, and neither needs a trace
    id to say so.
    """

    __slots__ = ("_obs", "_model_id", "_usage", "_meta", "_ended", "_started", "step")

    def __init__(
        self,
        observation: Any = None,
        model_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        step: str | None = None,
    ) -> None:
        self._obs = observation
        self._model_id = model_id
        self._usage: dict[str, int] | None = None
        # The wire step this call serves ("brain", "topic_classifier",
        # "retrieve", …) — the bucket its usage/cost aggregate under. Set by
        # `start_generation`; `record_response` folds into the ambient turn.
        self.step = step
        # Seeded with whatever the observation was *created* with, because
        # every later write sends the whole metadata map: Langfuse's `update()`
        # replaces the key rather than merging into it, so a partial map here
        # would silently drop `fallback_index`, `scrub_rule` and `saw_context`
        # the moment usage arrived.
        self._meta: dict[str, Any] = dict(metadata) if metadata else {}
        self._ended = False
        self._started = False

    def _fold_into_turn(self, details: dict[str, int], cost: dict[str, float] | None) -> None:
        """Roll this call's usage and cost into the ambient turn's accumulator.

        Fail-open by construction: this runs inside `record_response`'s try, so
        a corrupt accumulator can never break the transport. Generations
        created outside an activated turn (no ambient turn) simply accumulate
        nothing — the per-generation metadata still carries the numbers.
        """
        turn = _current_turn.get()
        if turn is None or self.step is None:
            return
        bucket = turn.usage.setdefault(self.step, {"input": 0, "output": 0, "total": 0})
        bucket["input"] += details.get("input", 0)
        bucket["output"] += details.get("output", 0)
        bucket["total"] += details.get("total", 0)
        turn.usage_seen = True
        if cost is not None:
            turn.cost[self.step] = turn.cost.get(self.step, 0.0) + cost["total"]
            turn.cost_seen = True
        else:
            turn.unpriced_seen = True

    # -- written by the transport -------------------------------------------
    def record_response(self, *, model: str | None = None, usage: Any = None) -> None:
        """The effective model id and the provider's token counts.

        `model` is read from the response body, not from what was requested:
        they differ whenever the endpoint resolves an alias, and "which model
        actually answered" is the question this whole field exists for.
        """
        if self._obs is None:
            return
        try:
            if model:
                self._model_id = model
            details = _usage_details(usage)
            update: dict[str, Any] = {}
            if model:
                update["model"] = model
            if details is not None:
                self._usage = details
                update["usage_details"] = details
                self._meta["usage_source"] = USAGE_PRESENT
                cost = _cost_details(self._model_id, details)
                if cost is not None:
                    update["cost_details"] = cost
                    self._meta["cost_source"] = COST_COMPUTED
                else:
                    self._meta["cost_source"] = COST_UNPRICED
                    log.warning(
                        "no MODEL_PRICES entry for %s — usage recorded, cost omitted",
                        self._model_id,
                    )
                self._fold_into_turn(details, cost)
            else:
                self._meta["usage_source"] = USAGE_ABSENT
            update["metadata"] = dict(self._meta)
            self._obs.update(**update)
        except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
            log.warning("could not record a model response (turn is unaffected): %s", exc)

    def note(self, **metadata: Any) -> None:
        """Record a fact the caller or the transport saw, without ending.

        Buffered into `_meta` rather than written straight through, so the
        single `update()` in `finish()` carries the whole metadata map — a
        partial map would overwrite what an earlier call put there.
        """
        if self._obs is None:
            return
        try:
            self._meta.update(metadata)
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("could not record trace metadata: %s", exc)

    def first_token(self) -> None:
        """Time to first token, set once, on the first streamed delta.

        TTFT is the number that explains a turn that *feels* sluggish while its
        total latency looks fine.
        """
        if self._obs is None or self._started:
            return
        self._started = True
        try:
            from datetime import datetime, timezone

            self._obs.update(completion_start_time=datetime.now(timezone.utc))
        except Exception as exc:  # noqa: BLE001 - fail open
            log.warning("could not record time to first token: %s", exc)

    # -- written by the caller ----------------------------------------------
    def finish(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        if self._obs is None or self._ended:
            return
        self._ended = True
        try:
            if metadata:
                self._meta.update(metadata)
            if self._meta.setdefault("usage_source", USAGE_ABSENT) == USAGE_ABSENT:
                self._meta.setdefault("cost_source", COST_UNPRICED)
            update: dict[str, Any] = {"metadata": dict(self._meta)}
            if output is not None:
                update["output"] = output
            if level is not None:
                update["level"] = level
            if status_message is not None:
                update["status_message"] = status_message
            self._obs.update(**update)
            self._obs.end()
        except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
            log.warning("could not finish a generation (turn is unaffected): %s", exc)

    def fail(self, exc: BaseException, *, metadata: dict[str, Any] | None = None) -> None:
        """End at ERROR, naming the exception class.

        The class name is the whole point: `detail:"degraded"` on the wire
        conflates an outage, a bad key and a truncated monologue, and today the
        cause lives only in CloudWatch (`trace-design.md` §4.5).
        """
        self.finish(
            metadata={**(metadata or {}), "degraded_reason": reason_for(exc)},
            level="ERROR",
            status_message=reason_for(exc),
        )


def reason_for(exc: BaseException) -> str:
    """An exception as a filterable literal, e.g. `HTTPStatusError:503`."""
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{name}:{status}" if status else name


NOOP_GENERATION = Generation()


def start_generation(
    name: str,
    model_id: str | None = None,
    *,
    params: dict[str, Any] | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    as_type: str = "generation",
    step: str | None = None,
) -> Generation:
    """Open a generation parented to whatever observation is ambient.

    No trace id is threaded: the turn span opened in `main._run_graph` is
    current for everything the graph task does, and Python contextvars carry it
    into every coroutine underneath. Threading `trace_id` through every
    `models.py` seam signature instead would break the one-line-monkeypatch
    property the whole test suite leans on (`trace-design.md` §4.2).

    `step` is the wire step this call serves and the bucket its usage/cost
    aggregates under on the turn summary. It defaults to the observation name,
    which is already the wire step for every judge (`validate_input`, …) and
    for `brain`; the one place they differ is retrieval, where `condense` and
    the embedding both serve `retrieve` and both pass it explicitly.

    Returns a no-op handle — never `None` — when tracing is down, so no call
    site has to branch.
    """
    if not _ENABLED or _client is None:
        return NOOP_GENERATION

    try:
        observation = _client.start_observation(
            name=name,
            as_type=as_type,
            model=model_id,
            model_parameters=params or None,
            input=input,
            metadata=dict(metadata) if metadata else None,
        )
        return Generation(observation, model_id, metadata, step or name)
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning("could not start a generation (turn is unaffected): %s", exc)
        return NOOP_GENERATION


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------

class Turn:
    """One turn's trace: the id on the wire, and the span everything hangs off.

    The `pipeline` span is created inside `activate()` — i.e. inside the graph
    task — so the contextvar that makes it ambient is task-local and cannot
    leak into a concurrent visitor's turn (KB-008). It ends with the graph;
    the trace-level fields are written by a separate, later span in
    `finalize_trace` (see `PIPELINE_SPAN_NAME` for why one span cannot do
    both).
    """

    __slots__ = (
        "trace_id",
        "url",
        "span",
        "usage",
        "cost",
        "usage_seen",
        "cost_seen",
        "unpriced_seen",
    )

    def __init__(self, trace_id: str | None = None, url: str | None = None) -> None:
        self.trace_id = trace_id
        self.url = url
        self.span: Any = None
        # Per-step accumulator for `finalize_trace`'s summary. `record_response`
        # folds into it via the ambient contextvar; created here, per request,
        # so it needs no reset and cannot outlive its turn.
        self.usage: dict[str, dict[str, int]] = {}
        self.cost: dict[str, float] = {}
        self.usage_seen = False
        self.cost_seen = False
        self.unpriced_seen = False

    @contextlib.contextmanager
    def activate(self) -> Iterator[None]:
        """Make this turn's span the ambient parent for the block.

        Entered and exited by hand rather than with a nested `with`, so a
        failure in either half is swallowed independently and neither can turn
        a tracing problem into a broken turn. Exceptions raised by the *body*
        propagate untouched — a graph failure is the graph's business.

        The same block also makes this turn the ambient accumulator target
        (`_current_turn`) for the block, so generations record their usage and
        cost into it with no id threaded. Both are reset on exit; a nested or
        failed activation restores whatever was current before.
        """
        manager = None
        token = None
        if _ENABLED and _client is not None and self.trace_id:
            try:
                token = _current_turn.set(self)
                manager = _client.start_as_current_observation(
                    name=PIPELINE_SPAN_NAME,
                    as_type="span",
                    trace_context=TraceContext(trace_id=self.trace_id),
                )
                self.span = manager.__enter__()
            except Exception as exc:  # noqa: BLE001 - fail open
                log.warning("could not open the turn span (turn is unaffected): %s", exc)
                manager = None
        try:
            yield
        finally:
            if token is not None:
                _current_turn.reset(token)
            if manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception as exc:  # noqa: BLE001 - fail open
                    log.warning(
                        "could not close the turn span (turn is unaffected): %s", exc
                    )


def start_turn(trace_id: str | None, url: str | None = None) -> Turn:
    """A `Turn` holder. Never raises; a `None` id makes every method a no-op."""
    return Turn(trace_id, url)


def start_trace(
    client_id: str,
) -> tuple[CallbackHandler | None, str | None, str | None]:
    """Open a trace for one turn: `(handler, trace_id, url)`.

    The id is generated locally, which is what lets `/ask` put the trace URL on
    the wire as the very first frame — before the graph has run, let alone
    finished. Minted with the SDK's own `create_trace_id()`: the v4 SDK
    silently discards a foreign id and stores the trace under its own, so a
    uuid4 here would hand out a link to a trace that will never exist (KB-019).

    Returns `(None, None, None)` when tracing is down; never raises.
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


def _scored(hits) -> list[dict[str, Any]]:
    return [
        {
            "url": getattr(hit, "url", None),
            "score": round(float(getattr(hit, "score", 0.0)), 4),
        }
        for hit in hits
    ]


def record_retrieval(
    trace_id: str | None,
    raw_query: str,
    condensed_query: str,
    fetched,
    kept,
) -> None:
    """Write the `retrieve` node's own span: what was searched for, and what
    came back — before *and* after the score floor.

    plan.md asks for "query, top-k hits, scores — all visible in the public
    trace", and each earns its place. **Both queries** are recorded because the
    *delta* between the visitor's words and the condenser's rewrite is the
    evidence that a bad rewrite happened at all; recording only the condensed
    form leaves that delta to be reconstructed from a state blob. The **URLs**
    are what the visitor may end up reading.

    **`fetched` vs `kept` is the point of this span.** The old docstring
    claimed scores were "the only way to tell 'the corpus had nothing' from
    'the floor is set too high'" — and that claim was false at the call site,
    which passed the list *after* `RETRIEVE_MIN_SCORE`, after the per-URL
    dedupe and after the top-k cut. A floor-suppressed retrieval therefore
    recorded `hits: []`, byte-identical to an empty corpus (PR #63 review
    comment 3; issue #70's dedupe made it a three-way ambiguity). Recording the
    pre-floor list alongside the surviving slate is what finally makes the
    docstring true. Cost: at most `RETRIEVE_FETCH_K` `{url, score}` pairs,
    ~1.6 KB.

    The chunk *text* is still deliberately not written: it is already in the
    trace's brain span as part of the system prompt, and duplicating a few
    thousand tokens per turn would make the public trace expensive to load for
    no new fact — and payload volume is quota (KB-021).

    Parented by ambient context when the turn span is active, which is what
    stops it being a root-level observation whose IO Langfuse would promote to
    the trace root (the §1.1 clobber). `trace_id` is still accepted as the
    fallback parent so the span survives if it is ever called outside the turn.
    No-op without one, and on any exception — a dropped span is never allowed
    to become a dropped turn.
    """
    if not _ENABLED or _client is None or trace_id is None:
        return

    try:
        fetched_scored = _scored(fetched)
        kept_scored = _scored(kept)
        _client.start_observation(
            name=RETRIEVAL_SPAN_NAME,
            as_type="span",
            input={"raw_query": raw_query, "condensed_query": condensed_query},
            output={"fetched": fetched_scored, "kept": kept_scored},
            metadata={
                "fetched_count": len(fetched_scored),
                "kept_count": len(kept_scored),
                "floor": config.RETRIEVE_MIN_SCORE,
                "top_k": config.RETRIEVE_TOP_K,
                "fetch_k": config.RETRIEVE_FETCH_K,
                "max_per_url": config.RETRIEVE_MAX_PER_URL,
                "condense_used": raw_query != condensed_query,
            },
        ).end()
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning("could not record the retrieval span (turn is unaffected): %s", exc)


def _tags(
    outcome: str,
    refused_step: str | None,
    degraded: bool,
    kb_state: str | None,
) -> list[str]:
    """Trace-level filters, and only filters.

    Each of these is a query someone ran by hand during fine-tuning.md Pass 1,
    when the trace list was 924 undifferentiated `turn` rows and the evidence
    traces were found by *timestamp*. Per-observation facts stay in metadata —
    tags are the trace's index, not its contents.
    """
    tags = [f"outcome:{outcome}"]
    if refused_step:
        tags.append(f"refused:{refused_step}")
    if degraded:
        tags.append("degraded")
    if kb_state:
        tags.append(f"kb:{kb_state}")
    return tags


def finalize_trace(
    turn: Turn,
    refused_step: str | None,
    step_latencies: dict[str, int],
    total_latency_ms: int,
    client_id: str,
    *,
    outcome: str = "answered",
    message: str = "",
    history_turns: int = 0,
    answer: str | None = None,
    refusal_text: str | None = None,
    degraded: bool = False,
    kb_state: str | None = None,
) -> dict[str, Any] | None:
    """Write the trace-level fields and flush. Must run before the terminal SSE
    event: Langfuse batches in a background thread and Lambda freezes the
    instance the moment the response ends, so an unflushed batch is a trace that
    silently never arrives.

    Returns the same numbers it writes to the span — per-step and per-turn
    tokens, cost and latency — so the caller can ride them onto the wire
    (`done`'s `summary` field, issue #109). One payload, two consumers: the
    trace and the transcript cannot disagree because they are the same dict.

    Returns `None` when the turn has no id (tracing was down at `start_trace`)
    and on any exception — a dropped trace is never allowed to become a dropped
    turn, and neither is a failed wire summary.
    """
    if not _ENABLED or _client is None or turn is None or turn.trace_id is None:
        return None

    try:
        # Order matters, and not for a tidy-code reason. The graph's spans and
        # this one both carry trace-level attributes; when they land in the same
        # export batch the LangChain root span wins the trace upsert and
        # `public`/`session_id` are silently dropped — verified against Langfuse
        # Cloud, where the trace came back `public: false` every time. Flushing
        # the graph's spans first makes this span's fields the later write.
        # `trace-design.md` §6 may retire this once the handler is gone; that is
        # Phase 2, and until then deleting it silently un-publishes every trace.
        _client.flush()

        with propagate_attributes(
            session_id=client_id,
            tags=_tags(outcome, refused_step, degraded, kb_state),
        ):
            # Created *here*, inside the block and after the flush, never
            # reusing the `pipeline` span the graph task opened. Both halves of
            # that matter and both were established by reading real traces
            # back: `propagate_attributes` only reaches spans created inside
            # it, and the trace-level upsert is won by the later-created write.
            # See `PIPELINE_SPAN_NAME`.
            span = _client.start_observation(
                name=TURN_SPAN_NAME,
                as_type="span",
                trace_context=TraceContext(trace_id=turn.trace_id),
            )
            # Per-step tokens and cost, plus a final aggregate. Same shape as
            # `latency_ms`: the step numbers are readable at a glance, and the
            # summary answers "what did this turn cost" without opening any
            # generation. The literals reuse the per-generation ones (KB-009):
            # a turn whose steps reported no usage reads as "absent", not as
            # broken instrumentation. `cost_usd` simply lacks a step that was
            # priced off the table, matching how per-generation `cost_details`
            # is omitted for an unpriced model.
            usage_tokens = {
                step: dict(bucket) for step, bucket in turn.usage.items()
            }
            cost_usd = dict(turn.cost)
            tokens_total = {"input": 0, "output": 0, "total": 0}
            for bucket in usage_tokens.values():
                tokens_total["input"] += bucket["input"]
                tokens_total["output"] += bucket["output"]
                tokens_total["total"] += bucket["total"]
            cost_total = sum(cost_usd.values())
            usage_source = USAGE_PRESENT if turn.usage_seen else USAGE_ABSENT
            if turn.cost_seen:
                cost_source = COST_COMPUTED
            elif turn.unpriced_seen:
                cost_source = COST_UNPRICED
            else:
                cost_source = USAGE_ABSENT
            # The wire payload. `step_cost_usd` is the per-step map (`cost_usd`
            # on the span metadata); the totals live under `tokens`/`cost_usd`
            # here, the same names `summary` uses on the span. One dict built
            # once, returned to the caller and written to the span — the
            # transcript and the trace share it verbatim.
            wire = {
                "latency_ms": total_latency_ms,
                "tokens": tokens_total,
                "cost_usd": cost_total,
                "usage_source": usage_source,
                "cost_source": cost_source,
                "usage_tokens": usage_tokens,
                "step_cost_usd": cost_usd,
            }
            span.update(
                metadata={
                    "refused_step": refused_step or NOT_REFUSED,
                    "latency_ms": dict(step_latencies),
                    "total_latency_ms": total_latency_ms,
                    "usage_tokens": usage_tokens,
                    "cost_usd": cost_usd,
                    "summary": {
                        key: wire[key]
                        for key in (
                            "latency_ms",
                            "tokens",
                            "cost_usd",
                            "usage_source",
                            "cost_source",
                        )
                    },
                }
            )
            # Explicitly, rather than by winning an upsert race. Langfuse
            # derives trace IO from root-level observation IO when nobody sets
            # it, which is how the retrieval span's `{query}`/`{hits}` came to
            # be what the public trace claimed the visitor asked and saw
            # (`trace-design.md` §1.1, verified on real traces). Setting it here
            # is deterministic. `set_trace_io` carries a deprecation notice in
            # 4.14.3 pointing at `propagate_attributes` for *other* trace
            # attributes; trace input/output has no replacement there, so this
            # remains the mechanism until one exists.
            trace_output: dict[str, Any] = {
                "outcome": outcome,
                "answer_chars": len(answer or ""),
            }
            if refusal_text:
                trace_output["refusal_text"] = refusal_text
            span.set_trace_io(
                input={"message": message, "history_turns": history_turns},
                output=trace_output,
            )
            # Public traces are the point: the URL on the wire has to open
            # for a visitor who has never seen this Langfuse project.
            span.set_trace_as_public()
            span.end()

        _client.flush()
        return wire
    except Exception as exc:  # noqa: BLE001 - fail open, see module docstring
        log.warning(
            "could not finalize trace %s (turn is unaffected): %s", turn.trace_id, exc
        )
        return None


_configure(
    public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(),
    secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "").strip(),
    host=os.environ.get("LANGFUSE_HOST", "").strip(),
    environment=os.environ.get("CADRE_ENV", "dev").strip() or "dev",
)
