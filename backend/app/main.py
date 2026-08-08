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
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app import config, sse, tracing
from app.graph.build import build_graph
from app.graph.emit import QueueEmitter
from app.graph.state import Turn, initial_state

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cadre")

ALLOWED_ORIGIN = os.environ.get("CADRE_ALLOWED_ORIGIN", "https://cadre.marcuss.pro")
IS_PROD = os.environ.get("CADRE_ENV", "dev") == "prod"

_DEV_ORIGINS = ["http://localhost:8088", "http://127.0.0.1:8088"]
CORS_ORIGINS = [ALLOWED_ORIGIN] if IS_PROD else [ALLOWED_ORIGIN, *_DEV_ORIGINS]

app = FastAPI(title="cadre", docs_url=None, redoc_url=None)
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


async def _run_graph(state, emit, queue: asyncio.Queue, handler=None) -> None:
    """Drive the graph, then post the terminal the generator should emit."""
    try:
        # Both per-request objects ride the invocation's config, never the state
        # channel (KB-008): `emit` is a live queue and `handler` is bound to one
        # visitor's trace, and a checkpointable channel is how one visitor's
        # stream — or trace — ends up in another's. `callbacks` is LangChain's
        # own per-invocation callback list, which is what attaches the handler
        # to the graph run and gets every node as a span for free; there is
        # deliberately no per-call-site Langfuse logging in `nodes.py`.
        cfg: dict = {"configurable": {"emit": emit}}
        if handler is not None:
            cfg["callbacks"] = [handler]

        final = await GRAPH.ainvoke(state, config=cfg)
        await queue.put(
            (
                "done",
                {
                    "outcome": final.get("outcome", "answered"),
                    "refusal_text": final.get("refusal_text"),
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
    task = asyncio.create_task(_run_graph(state, emit, queue, handler))

    # What the trace needs, harvested from the events already going out. The
    # per-step numbers are the same `elapsed_ms` the stepper renders — reused,
    # not re-measured, so the trace and the transcript cannot disagree.
    step_latencies: dict[str, int] = {}
    refused_step: str | None = None

    def _finalize() -> None:
        """Flush before the terminal frame, on every terminal path: Lambda
        freezes the instance the moment the response ends, so a batch that is
        still in Langfuse's background thread is a trace nobody ever sees."""
        tracing.finalize_trace(
            trace_id,
            refused_step,
            step_latencies,
            round((time.monotonic() - started) * 1000),
            req.conversation_id,
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
                yield sse.state(
                    payload["step"],
                    payload["status"],
                    payload["detail"],
                    payload["elapsed_ms"],
                )
            elif kind == "token":
                yield sse.token(payload)
            elif kind == "done":
                _finalize()
                yield sse.done(payload["outcome"], payload["refusal_text"])
                return
            elif kind == "error":
                # Terminal on its own — no `done` follows an `error`.
                _finalize()
                yield sse.error(config.ERROR_TEXT)
                return

            # Flush each event as its own chunk rather than coalescing into one
            # write at the end.
            await asyncio.sleep(0)
    finally:
        # A visitor who closed the tab should not leave a turn running.
        if not task.done():
            task.cancel()
