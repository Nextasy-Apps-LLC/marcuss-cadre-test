"""Cadre backend — walking skeleton.

Deliberately minimal: `ping` answers `pong`, everything else gets a stub. What
it *does* implement in full is the SSE contract, so the React client, the
CloudFront streaming path, and the deploy pipeline can all be proven
end-to-end before any model is wired in. Replacing `_reply_for()` with real
inference is then a change to one function rather than to the whole shape.

    POST /ask      → SSE stream of rail / token / done events
    GET  /healthz  → liveness probe (CloudFront + deploy smoke test)
    GET  /config   → greeting and suggestion chips for the page
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import sse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cadre")

MAX_INPUT_LEN = 2000

ALLOWED_ORIGIN = os.environ.get("CADRE_ALLOWED_ORIGIN", "https://cadre.marcuss.pro")
IS_PROD = os.environ.get("CADRE_ENV", "dev") == "prod"

_DEV_ORIGINS = ["http://localhost:8088", "http://127.0.0.1:8088"]
CORS_ORIGINS = [ALLOWED_ORIGIN] if IS_PROD else [ALLOWED_ORIGIN, *_DEV_ORIGINS]

GREETING = "Say `ping` and I'll say `pong`. That's the whole skeleton for now."
SUGGESTIONS = ["ping"]

STUB_REPLY = "I only know `ping` so far — say that and I'll answer."

# Streamed in fragments so the client exercises its incremental-render path.
# A single-chunk reply would let a broken token handler pass unnoticed.
CHUNK_SIZE = 8

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

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


class AskRequest(BaseModel):
    conversation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    message: str

    @field_validator("message")
    @classmethod
    def _message_shape(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be empty")
        if len(v) > MAX_INPUT_LEN:
            raise ValueError("message too long")
        if _CONTROL_CHARS.search(v):
            raise ValueError("message contains control characters")
        return v


def _reply_for(message: str) -> str:
    """The entire brain, for now.

    This is the seam. Everything around it — rails, streaming, transport — is
    already the real shape, so wiring in a model means replacing this function
    and nothing else.
    """
    return "pong" if message.strip().lower() == "ping" else STUB_REPLY


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/config")
async def page_config() -> JSONResponse:
    """Greeting and chips live server-side so they cannot drift from what the
    backend actually answers."""
    return JSONResponse({"greeting": GREETING, "suggestions": SUGGESTIONS})


@app.post("/ask")
async def ask(request: Request) -> StreamingResponse:
    try:
        body = await request.json()
        req = AskRequest.model_validate(body)
    except (ValidationError, ValueError):
        return StreamingResponse(
            _reject("rail1:invalid_request"),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return StreamingResponse(
        _stream(req.message),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _reject(reason: str) -> AsyncIterator[str]:
    started = time.monotonic()
    yield sse.rail("rail1", "input_validation", False, 0.0, reason.split(":", 1)[1])
    yield sse.done(True, reason, (time.monotonic() - started) * 1000)


async def _stream(message: str) -> AsyncIterator[str]:
    started = time.monotonic()

    try:
        # All six rails pass trivially at this stage. They are emitted anyway
        # because the client renders the trace panel from them — a skeleton
        # that skipped them would leave six rails spinning forever and hide
        # exactly the integration bug this endpoint exists to catch.
        for rail_id, rail_name in sse.RAILS:
            t0 = time.monotonic()
            reason = "response_ready" if rail_id == "rail4" else "ok"
            yield sse.rail(rail_id, rail_name, True, (time.monotonic() - t0) * 1000, reason)
            # Hand control back so each event flushes as its own chunk rather
            # than coalescing into one write at the end.
            await asyncio.sleep(0)

        for chunk in _chunks(_reply_for(message), CHUNK_SIZE):
            yield sse.token(chunk)
            await asyncio.sleep(0)

        yield sse.done(False, None, (time.monotonic() - started) * 1000)

    except Exception as exc:  # noqa: BLE001
        log.exception("stream failed: %s", exc)
        yield sse.error("Something went wrong. Please try again.")
