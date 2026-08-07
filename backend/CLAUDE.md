# backend/CLAUDE.md — FastAPI backend guidelines

FastAPI + uvicorn in an arm64 container Lambda behind the AWS Lambda Web
Adapter. Deliberately a walking skeleton: the SSE plumbing is real end-to-end,
the brain is a stub. Rules, each with its why:

## The SSE contract (`app/sse.py`)

- `sse.py` is the single source of truth for the wire format — four events (`rail`, `token`, `done`, `error`) and the six-rail `RAILS` order. `web/src/types.ts` mirrors it verbatim; nothing imports across the boundary, so renaming a field is a silent breaking change. Change both sides in one PR or neither.
- All six rails are emitted before any token — the client paints the trace panel from rail events, and tokens-first would leave the panel blank when the answer appears. `done` is always the terminal event.
- Replies stream in `CHUNK_SIZE` fragments with `await asyncio.sleep(0)` between yields — a single-chunk reply would let a broken client token handler pass unnoticed, and the sleep(0) flushes each event as its own chunk instead of one write at the end.

## Request handling (`app/main.py`)

- Validation failures become SSE refusals (rail1 fail + `done{refused:true}`), never HTTP 4xx — the browser renders them through its normal `done` path instead of falling into its offline branch. Refusal reasons name the rail (`rail1:…`).
- `_reply_for()` is the deliberate seam for the future Bedrock brain: everything around it — rails, streaming, transport — is already the real shape, so wiring in a model means replacing that one function and nothing else. Don't restructure around it.
- `MAX_INPUT_LEN = 2000` is mirrored by the web composer's cap; keep them equal. Validation also rejects control characters and malformed `conversation_id`s — through the same refusal path, so a fuzzer can't crash the stream.
- SSE responses carry `Cache-Control: no-cache, no-transform` (plus `X-Accel-Buffering: no`) — a cached SSE response is a stream that never streams, and `no-transform` stops proxies buffering to re-encode.
- Mid-stream failures become a generic `sse.error` event, never a traceback on the wire — the 200 status is already committed by then, and details belong in `log.exception`, not in what a visitor sees.
- CORS: `CADRE_ENV=prod` narrows allowed origins to `CADRE_ALLOWED_ORIGIN` alone — the localhost origins are dev-only and must never ship.
- `docs_url=None, redoc_url=None` stays: three routes need no auto-docs, and the interactive docs would be a public, unauthenticated surface behind CloudFront.
- `/config` serves the greeting and suggestion chips server-side so they cannot drift from what the backend actually answers — the tests assert every advertised suggestion gets a real reply, because a refused chip is the worst first impression. `/healthz` is the CloudFront + deploy smoke probe; keep it dependency-free.

## Runtime (`Dockerfile`)

- `ENTRYPOINT []` is load-bearing: the AWS Lambda base image's entrypoint treats CMD[0] as a Python handler name and swallows the uvicorn command, crashing init. CI builds the image but never boots it, so this class of bug only surfaces on invoke — smoke with `docker run -p 8080:8080` when touching the Dockerfile.
- `AWS_LWA_INVOKE_MODE=response_stream` is the whole reason the stack streams — buffered mode waits for a complete body and every SSE event arrives at once at the end. It only takes effect behind a RESPONSE_STREAM Function URL.
- Single uvicorn worker (`--workers 1`) — a Lambda invoke serves one request; extra workers buy nothing and cost cold-start memory.
- The adapter turns the invoke into ordinary HTTP against uvicorn on :8080, so the same image runs unchanged locally and in Lambda — keep it that way; no Lambda-only code paths.
- Runtime deps are exactly `requirements.txt`'s three (fastapi, uvicorn, pydantic). Don't add more for the skeleton — every dependency is cold-start weight.

## Verifying

- `pytest` from `backend/` — `tests/test_ask.py` drives the real ASGI app and asserts rail order, rails-before-tokens, chunked emission, cache headers, and the refusal shape. Treat it as the executable form of the contract.
