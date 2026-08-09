"""Cadre backend — the LangGraph conversation engine behind SSE protocol v2.

    POST /ask      → SSE stream of state / token / done / error events
    GET  /healthz  → liveness probe (CloudFront + deploy smoke test)
    GET  /config   → greeting and suggestion chips for the page

`/ask` runs the graph as a task and streams whatever the nodes emit onto an
`asyncio.Queue` that the response generator drains. That indirection is what
makes the pipeline visible in real time: the alternative — collecting the
graph's result and then describing it — would paint the whole stepper at the
instant the answer was already finished.

Model calls are seams (`app/graph/models.py`) until Phase 1b, so this file is
already the final shape of the request path.

The `lifespan` hook is the other half of that principle applied to time rather
than to visibility: anything a container must do exactly once belongs in init,
where Lambda gives full-CPU burst and no visitor is waiting, not in whichever
request happens to be first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app import config, kb, sse, tracing
from app.graph.build import build_graph
from app.graph.emit import QueueEmitter
from app.graph.state import Turn, initial_state

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cadre")

ALLOWED_ORIGIN = os.environ.get("CADRE_ALLOWED_ORIGIN", "https://cadre.marcuss.pro")
IS_PROD = os.environ.get("CADRE_ENV", "dev") == "prod"

_DEV_ORIGINS = ["http://localhost:8088", "http://127.0.0.1:8088"]
CORS_ORIGINS = [ALLOWED_ORIGIN] if IS_PROD else [ALLOWED_ORIGIN, *_DEV_ORIGINS]

def _warm_kb() -> None:
    """Pay the KB's one-off open cost here, where nobody is waiting.

    `app/kb/store.py` opens the corpus once per process behind `lru_cache`.
    That was always right; what was wrong is *when* the once happened. The
    deferred `import lancedb` (tens of MB of native extension), the
    `lancedb.connect()`, the `open_table()` and the Arrow schema read were all
    executed by whichever visitor asked the first question — measured on prod
    as `retrieve` costing 9661 ms cold against 548 ms warm (issue #67).

    Lambda's INIT phase runs at full-CPU burst and completes before the
    function is handed any traffic, and the Lambda Web Adapter boots uvicorn —
    and so the ASGI lifespan — inside that window. Work moved here is
    therefore off every visitor's turn budget (KB-004) rather than merely
    faster.

    `kb.available()` is the entry point on purpose: it runs the whole
    `ensure_ready()` gate, which is what triggers the import, the connect, the
    open and the schema read, and it is already the never-raising path — a
    missing, unreadable or mismatched artifact is a `False`, not an exception.
    Blocking the loop here is deliberate: nothing else is running at init, and
    the point is that the work is *finished* before the first request arrives.
    """
    started = time.monotonic()
    try:
        ready = kb.available()
    except Exception as exc:  # noqa: BLE001
        # `available()` promises not to raise; this hook does not get to rely on
        # that promise. An init that crashes is a Lambda that fails on invoke
        # (KB-001) — infinitely worse than the slow first turn it replaces.
        elapsed = round((time.monotonic() - started) * 1000)
        log.warning(
            "KB warm-up failed after %d ms (%s) — serving anyway; retrieval will "
            "report skipped",
            elapsed,
            exc,
            exc_info=True,
        )
        return

    elapsed = round((time.monotonic() - started) * 1000)
    if ready:
        log.info(
            "KB warm-up: ready in %d ms — lancedb imported, table and manifest "
            "open before the first request",
            elapsed,
        )
    else:
        # Not a fault: no artifact, the kill switch, or a dimension mismatch.
        # `retrieve` will say which on the wire; this line is what stops the
        # degradation being silent (KB-009).
        log.warning(
            "KB warm-up: unavailable after %d ms — retrieval will report skipped "
            "for this container",
            elapsed,
        )


def _log_model_overrides() -> None:
    """Say, once per container, when the environment has moved a model slot.

    A `CADRE_MODEL_*` override is break-glass (issue #84): it replaces a model
    that was benchmarked against the prompts in this image with one that was
    not, and because every model step fails open the result renders as a
    perfectly healthy chat (KB-009). The deploy gate stops that reaching
    production; this line is what makes it visible in CloudWatch on the paths
    the gate cannot see — a hand-set override during an incident.
    """
    overrides = config.model_overrides()
    if not overrides:
        return
    for slot, (expected, effective) in sorted(overrides.items()):
        log.warning(
            "model override: %s is running %s, not the %s this image was built "
            "and benchmarked with — remove the %s environment variable to "
            "restore it",
            slot,
            effective,
            expected,
            config.MODEL_ENV_VARS[slot],
        )


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Container init. Runs before uvicorn accepts anything."""
    _log_model_overrides()
    _warm_kb()
    yield


app = FastAPI(title="cadre", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "accept"],
)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# Compiled once: the graph is stateless between turns and compiling it per
# request would put graph construction inside every visitor's turn budget.
GRAPH = build_graph()


class AskRequest(BaseModel):
    """Deliberately permissive.

    Only the *shape* is checked here; every content rule (length, control
    characters, the id format, the rate limit) belongs to the `validate_input`
    node, so a refusal always arrives as a `state` event on the stream instead
    of as an HTTP 422 the browser would render through its offline branch.
    """

    conversation_id: str
    message: str
    history: list[Turn] = []


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/config")
async def page_config() -> JSONResponse:
    """Greeting and chips live server-side so they cannot drift from what the
    backend actually answers."""
    return JSONResponse({
        "greeting": config.GREETING,
        "suggestions": config.SUGGESTIONS,
        "step_models": config.STEP_MODELS,
    })


@app.post("/ask")
async def ask(request: Request) -> StreamingResponse:
    try:
        body = await request.json()
        req = AskRequest.model_validate(body)
    except (ValidationError, ValueError, TypeError):
        # A body the graph cannot even be handed still owes the client the
        # same wire sequence as any other refusal.
        return StreamingResponse(
            _malformed_payload_stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return StreamingResponse(
        _stream(req),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _malformed_payload_stream() -> AsyncIterator[str]:
    yield sse.state("validate_input", "running")
    yield sse.state("validate_input", "fail", "malformed_payload")
    for step in sse.unreported({"validate_input"}):
        yield sse.state(step, "skipped")
    yield sse.done("refused", config.REFUSAL_TEXTS["validate_input"])


async def _run_graph(state, emit, queue: asyncio.Queue, handler=None, turn=None) -> None:
    """Drive the graph, then post the terminal the generator should emit."""
    try:
        # Both per-request objects ride the invocation's config, never the state
        # channel (KB-008): `emit` is a live queue and `handler` is bound to one
        # visitor's trace, and a checkpointable channel is how one visitor's
        # stream — or trace — ends up in another's. `callbacks` is LangChain's
        # own per-invocation callback list, which is what attaches the handler
        # to the graph run and gets every node as a span for free.
        cfg: dict = {"configurable": {"emit": emit}}
        if handler is not None:
            cfg["callbacks"] = [handler]

        # The turn span is opened *here*, inside the task, and not in `_stream`.
        # That is the whole isolation argument: a contextvar set inside a task
        # belongs to that task, so the generations the transport creates deep
        # inside the graph parent themselves correctly without a single trace id
        # being threaded through `models.py` — and two concurrent visitors can
        # never see each other's span (the same concern as KB-008).
        #
        # It closes before the terminal goes on the queue, so `finalize_trace`
        # in `_stream` cannot race the graph's own spans.
        turn_ctx = turn.activate() if turn is not None else contextlib.nullcontext()
        with turn_ctx:
            final = await GRAPH.ainvoke(state, config=cfg)

        await queue.put(
            (
                "done",
                {
                    "outcome": final.get("outcome", "answered"),
                    "refusal_text": final.get("refusal_text"),
                    "answer": final.get("answer") or "",
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        # The 200 is long committed by now, so a failure is an `error` event —
        # generic on the wire, detailed in the log.
        log.exception("graph failed: %s", exc)
        await queue.put(("error", None))


async def _stream(req: AskRequest) -> AsyncIterator[str]:
    started = time.monotonic()

    # The trace id is generated locally, so the link is knowable before any work
    # happens — which is why this is the first frame of the response rather than
    # something appended at the end. When tracing is down there is no frame at
    # all: the client sees one fewer chip, never a broken or half-filled one.
    handler, trace_id, trace_url = tracing.start_trace(req.conversation_id)
    if trace_url:
        yield sse.trace(trace_id, trace_url)

    queue: asyncio.Queue = asyncio.Queue()
    emit = QueueEmitter(queue, trace_id=trace_id)
    state = initial_state(req.message, req.history, req.conversation_id)
    turn = tracing.start_turn(trace_id, trace_url)
    task = asyncio.create_task(_run_graph(state, emit, queue, handler, turn))

    # What the trace needs, harvested from the events already going out. The
    # per-step numbers are the same `elapsed_ms` the stepper renders — reused,
    # not re-measured, so the trace and the transcript cannot disagree. The
    # same applies to the tags: `degraded` and `kb:*` are read off the wire
    # rather than recomputed, so a trace filter and a stepper chip cannot
    # disagree about whether a rail was down.
    step_latencies: dict[str, int] = {}
    refused_step: str | None = None
    degraded = False
    kb_state: str | None = None

    def _finalize(outcome: str, refusal_text: str | None, answer: str) -> dict[str, Any] | None:
        """Flush before the terminal frame, on every terminal path: Lambda
        freezes the instance the moment the response ends, so a batch that is
        still in Langfuse's background thread is a trace nobody ever sees.

        Returns whatever `finalize_trace` computed so the caller can put it on
        the wire (`done`'s `summary`, issue #109) — `None` when tracing was
        down or no-op'd."""
        return tracing.finalize_trace(
            turn,
            refused_step,
            step_latencies,
            round((time.monotonic() - started) * 1000),
            req.conversation_id,
            outcome=outcome,
            message=req.message,
            history_turns=len(req.history),
            answer=answer,
            refusal_text=refusal_text,
            degraded=degraded,
            kb_state=kb_state,
        )

    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=config.PING_INTERVAL_S
                )
            except (asyncio.TimeoutError, TimeoutError):
                # Nothing from the graph for a while: keep intermediaries from
                # reaping a connection that is merely waiting on a slow step.
                yield sse.ping()
                continue

            if kind == "state":
                if payload["elapsed_ms"] is not None:
                    step_latencies[payload["step"]] = payload["elapsed_ms"]
                if payload["status"] == "fail":
                    refused_step = payload["step"]
                if payload["detail"] == "degraded":
                    degraded = True
                if payload["step"] == "retrieve":
                    # Three distinct states, and the tag has to keep them
                    # distinct: the KB ran and found something, ran and found
                    # nothing, or never ran. Collapsing the last two is what
                    # made incident 1 unfindable.
                    if payload["status"] == "skipped":
                        kb_state = "skipped"
                    elif payload["detail"] == "no_hits":
                        kb_state = "no_hits"
                    else:
                        kb_state = "hit"
                yield sse.state(
                    payload["step"],
                    payload["status"],
                    payload["detail"],
                    payload["elapsed_ms"],
                    payload["retrieval"],
                )
            elif kind == "token":
                yield sse.token(payload)
            elif kind == "done":
                summary = _finalize(
                    payload["outcome"], payload["refusal_text"], payload.get("answer", "")
                )
                yield sse.done(payload["outcome"], payload["refusal_text"], summary=summary)
                return
            elif kind == "error":
                # Terminal on its own — no `done` follows an `error`.
                _finalize("error", None, "")
                yield sse.error(config.ERROR_TEXT)
                return

            # Flush each event as its own chunk rather than coalescing into one
            # write at the end.
            await asyncio.sleep(0)
    finally:
        # A visitor who closed the tab should not leave a turn running.
        if not task.done():
            task.cancel()
