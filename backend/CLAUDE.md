# backend/CLAUDE.md — FastAPI backend guidelines

FastAPI + uvicorn in an arm64 container Lambda behind the AWS Lambda Web
Adapter, wrapping a **LangGraph conversation engine**. The graph is the
backend: every step of the guarded pipeline is a node, every terminal is an
explicit outcome, and the SSE stream is a live projection of the graph's
progress. Rules, each with its why:

## The engine (`app/graph/`)

- `build.py` wires plan.md's diagram and nothing else: `validate_input →
  injection_check → topic_classifier → retrieve → brain → output_safety`, with
  any check fail routing to `refuse`, `topic_classifier`'s `needs_human`
  routing to `escalate`, and both terminals plus an `output_safety` pass ending
  the run. A new step means a node and an edge here — never a branch buried
  inside another node, because a branch that isn't an edge is invisible to both
  the stepper and the trace.
- Nodes are `async (state, emit) -> state` in `nodes.py`. They emit `running`
  before they work and their verdict the moment they have one; a node that
  reports only at the end turns the browser's stepper into a replay.
- `emit` is passed per request through `config["configurable"]`, never through
  the state channel — a live queue in a checkpointable channel is how one
  visitor's stream leaks into another's.
- `models.py` holds the four model seams (`judge_injection`, `classify_topic`,
  `guard_output`, `stream_reply`). Nodes call them **through the module** so a
  single monkeypatch swaps them; that is what keeps routing, streaming and the
  wire contract provable offline.
- **Model-backed checks fail open**: a seam that raises is logged and passes
  with `detail:"degraded"`, so an outage degrades observability, never a
  visitor's turn — and the client renders degraded amber, never green. `brain`
  is the exception: there is no answer to degrade to, so its failure propagates
  and becomes a terminal `error`.
- `validate_input` stays **deterministic** (rate limit, id shape, blank,
  length, control characters). A check that must fail closed on a hostile
  payload has no business needing a network call; the model-backed half of
  input safety is `injection_check`.
- `ConversationState` (`state.py`) is the only thing that crosses nodes, and
  `steps[]` on it is the same set of facts the client is told — a trace and a
  transcript that can disagree are worse than either alone.

## The SSE contract — protocol v2 (`app/sse.py`)

- `sse.py` is the single source of truth for the wire format. `web/src/types.ts`
  mirrors it verbatim; nothing imports across the boundary, so renaming a field
  is a silent breaking change (KB-005). Change both sides in the same phase —
  backend and web ship as coordinated PRs, and neither reaches a deployed state
  without the other.
- `STEPS = ["validate_input","injection_check","topic_classifier","retrieve",
  "brain","output_safety"]` — the client paints one chip per entry up front.
- Four events: `state {step, status: running|pass|fail|skipped, detail}`,
  `token {text}` (only while `brain` runs or the escalation text streams),
  `done {outcome: answered|refused|escalated|error, refusal_text}`, and
  `error {message}`. `done` is always terminal; `error` is terminal on its own
  and is never followed by a `done`.
- **Skips are server-authoritative.** On a terminal refusal or escalation the
  server emits `state{status:"skipped"}` for every step that never reported,
  before `done`. v1 left the client inferring skips from silence; it no longer
  has to guess.
- `detail` is the machine-readable reason — the failing check (`off_topic`,
  `rate_limited`, `kb_not_wired`, …) or `degraded` on a fail-open pass.
- `: ping` comment frames go out every `config.PING_INTERVAL_S` while a step is
  slow. They stop intermediaries reaping an idle-looking connection; they do
  **not** extend CloudFront's hard 60s cap (KB-004), which is the budget for
  the whole turn.
- Replies stream in `CHUNK_SIZE` fragments with `await asyncio.sleep(0)`
  between yields — a single-chunk reply would let a broken client token handler
  pass unnoticed, and the sleep(0) flushes each event as its own chunk instead
  of one write at the end.

## Request handling (`app/main.py`)

- `/ask` runs the compiled graph as a task and streams what the nodes emit onto
  an `asyncio.Queue` the response generator drains. Collecting the graph's
  result and then describing it would paint the entire stepper at the instant
  the answer was already finished.
- The graph is compiled **once at import**. It carries no state between turns,
  and compiling per request would spend a visitor's turn budget on graph
  construction.
- `AskRequest` validates **shape only**; every content rule belongs to the
  `validate_input` node. Validation failures become SSE refusals (a
  `validate_input` fail + skips + `done{outcome:"refused"}`), never HTTP 4xx —
  the browser renders them through its normal `done` path instead of falling
  into its offline branch. A body that will not parse at all gets the identical
  wire sequence with `detail:"malformed_payload"`.
- `config.MAX_INPUT_LEN = 2000` is mirrored by the web composer's cap; keep
  them equal.
- User-facing copy lives in `app/config.py` (`REFUSAL_TEXTS` keyed by the step
  that refused, `ESCALATION_TEXT` carrying the booking link, greeting, chips) —
  the graph never inlines a sentence, so the copy can be reviewed without
  reading the engine.
- SSE responses carry `Cache-Control: no-cache, no-transform` (plus
  `X-Accel-Buffering: no`) — a cached SSE response is a stream that never
  streams, and `no-transform` stops proxies buffering to re-encode.
- Mid-stream failures become a generic `sse.error` event, never a traceback on
  the wire — the 200 status is already committed by then, and details belong in
  `log.exception`, not in what a visitor sees.
- CORS: `CADRE_ENV=prod` narrows allowed origins to `CADRE_ALLOWED_ORIGIN`
  alone — the localhost origins are dev-only and must never ship.
- `docs_url=None, redoc_url=None` stays: three routes need no auto-docs, and
  the interactive docs would be a public, unauthenticated surface behind
  CloudFront.
- `/config` serves the greeting and suggestion chips server-side so they cannot
  drift from what the backend actually answers — the tests assert every
  advertised suggestion gets a real reply, because a refused chip is the worst
  first impression. `/healthz` is the CloudFront + deploy smoke probe; keep it
  dependency-free.
- `app/ratelimit.py` is an in-process sliding window per client id, swept on
  every call so an instance that saw a thousand one-shot visitors doesn't keep
  a thousand buckets alive. Per instance by design (plan.md defers a
  distributed token bucket); it is a floor on abuse, not accounting.

## The brain (`app/llm.py`, `app/graph/models.py`, `app/persona.py`)

- All LLM/Bedrock calls go through LangChain (`langchain-aws`'s
  `ChatBedrockConverse`), never raw `boto3` `bedrock-runtime` calls — one
  orchestration layer gives the pipeline composable chains, a single callback
  surface for tracing, and consistent tool-calling instead of hand-rolled
  invoke shapes. (LangChain/LangGraph is a coding standard set here, not an ADR
  decision — nothing in ADR 0001 constrains the orchestration library.)
- `app/llm.py`'s `chat_model()` is the only place a Bedrock client is
  constructed. Callers pass a model id and a token budget and nothing else, so
  the region, the sampling-parameter rules and (in Phase 2) the callback
  handler are set once rather than six times. Read every response through
  `llm.text_of()` — `ChatBedrockConverse` returns a bare `str` for some models
  and a list of typed blocks for others, and which you get depends on the
  model, not the call.
- **Sampling parameters are not universal.** Claude Opus 5 removed
  `temperature`/`top_p`/`top_k`; `langchain-aws` does not error on one, it logs
  `Model … does not support temperature; ignoring the provided value` and drops
  it. `chat_model()` therefore refuses to send `temperature` to a model that
  would discard it, so "temperature=0" never silently means "whatever the model
  felt like".
- **Model ids are checked before an image is pushed, not at the first
  request** — `scripts/assert_models.py`, wired into `deploy.yml` ahead of the
  build. It asserts both that a model exists *and* that the account is
  authorised to invoke it, because a fresh account lists the whole catalogue
  while being authorised for none of it. Every model step fails open, so a
  wrong or unauthorised id ships as a working-looking chat with amber rails
  rather than as a crash — the assertion is what keeps that from being
  discovered by a visitor.
- The `us.` prefix on the Anthropic ids in `app/config.py` is load-bearing:
  those models report `inferenceTypesSupported: [INFERENCE_PROFILE]`, so the
  bare `anthropic.claude-…` id is not invokable. The open-weight judges are
  `ON_DEMAND` and take their bare ids. `infra/lambda.tf` grants both ARN
  shapes for exactly this reason.
- Judges answer in one token and are parsed tolerantly (case, whitespace,
  punctuation, markdown). A response that is *not* a verdict degrades — it is
  never guessed at. The topic classifier's fallback chain is walked on model
  **errors only**: a model that answered is a model that is up, and re-asking
  costs a slice of the 55s turn budget for a question the output guard already
  backstops.
- `guard_output` is two independent halves. `scrub_failure()` is deterministic
  (URL allowlist — cadreai.com only — plus PII patterns), runs first, and has
  no outage mode; the Haiku judgement runs second and may degrade. A guard
  outage must never be able to leak an external URL.
- `app/persona.py` is the vetted baseline and the only source of facts until
  retrieval lands in Phase 3. Nothing else may state a fact about Cadre AI —
  no prices, no named clients, no invented capabilities — and the prompt says
  so explicitly rather than leaving it implied. `config.py` re-exports its
  `GREETING`/`SUGGESTIONS`/`CONTACT_URL` rather than restating them, so the
  copy `/config` advertises and the persona that must answer for it cannot
  drift. The dependency runs one way: persona never imports config.
- `config.BRAIN_MAX_TOKENS` bounds generation so the turn fits CloudFront's 60s
  origin cap (KB-004); the persona asks for the same brevity, so answers end
  rather than get truncated. Raising one without the other buys a cut-off
  sentence.

### Tracing (Phase 2 — not built yet)

Deferred with the rest of Langfuse; the standards are fixed now so the PR that
adds it has nothing left to decide:

- Every graph invocation emits a Langfuse trace, attached as Langfuse's
  LangChain `CallbackHandler` on the graph run — tracing rides the callback
  surface the orchestration already has, not bespoke logging per call site.
- Langfuse credentials follow ADR 0001's pre-agreed pattern (decision 4,
  mirrored in `infra/README.md`): an `aws_ssm_parameter` (`SecureString`,
  `value = "SET_OUT_OF_BAND"`, `lifecycle { ignore_changes = [value] }`, real
  value via `aws ssm put-parameter`), read **once at container start** — never
  per-request (SSM latency comes out of the 60s turn budget) and never a plain
  Lambda env var (env vars are Terraform-owned config, deliberately out of the
  deploy role's reach; secrets don't belong there).
- A trace MUST carry at minimum: the `client_id` (as the Langfuse session id,
  so a visitor's turns group), which step refused if any, and per-step + total
  `latency_ms` — a refusal you can't attribute to a step is undebuggable, and
  debuggable refusals are the product.
- Flush before `done`: Langfuse batches events in a background thread and
  Lambda freezes the instance the moment the response ends, so an unflushed
  batch is a silently dropped trace. Flush before yielding the terminal event.
- Tracing is fail-open — a Langfuse outage degrades observability, never the
  turn, the same posture as a degraded step verdict.

## Runtime (`Dockerfile`)

- `ENTRYPOINT []` is load-bearing: the AWS Lambda base image's entrypoint
  treats CMD[0] as a Python handler name and swallows the uvicorn command,
  crashing init (KB-001). CI builds the image but never boots it, so this class
  of bug only surfaces on invoke — smoke with `docker run -p 8080:8080` when
  touching the Dockerfile.
- `AWS_LWA_INVOKE_MODE=response_stream` is the whole reason the stack streams —
  buffered mode waits for a complete body and every SSE event arrives at once
  at the end. It only takes effect behind a RESPONSE_STREAM Function URL.
- Single uvicorn worker (`--workers 1`) — a Lambda invoke serves one request;
  extra workers buy nothing and cost cold-start memory.
- The adapter turns the invoke into ordinary HTTP against uvicorn on :8080, so
  the same image runs unchanged locally and in Lambda — keep it that way; no
  Lambda-only code paths.
- Runtime dependencies are exactly `requirements.txt`. Every dependency is
  cold-start weight, so each addition is justified in the PR that adds it —
  `langgraph` and `langchain-core` came in with the engine, `langchain-aws`
  with the model steps, and `langfuse` arrives with tracing in Phase 2.
- The image copies `app/` only. `scripts/` runs in CI and on a laptop against a
  real account; shipping it (and its `boto3` import) into the runtime would be
  cold-start weight for code no invoke ever executes.

## Verifying

- `pytest` from `backend/` — the unit suite drives the real ASGI app with the
  model seams patched and asserts step ordering, server-authoritative skips,
  chunked emission, cache headers, every terminal and the refusal shape. Treat
  it as the executable form of the contract.
- `pytest -m e2e` with `BASE_URL` set runs `tests/e2e/` against a real target
  (default: the image in local docker). It is excluded from the default run —
  turns cost real money once the seams are live. See `tests/e2e/README.md`.
- A curl-green smoke test does not prove streaming works (KB-007): for anything
  touching the stream, watch tokens land in a browser tab before calling it
  tested.
