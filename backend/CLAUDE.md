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
- Five events. `trace {trace_id, url}` comes first when tracing is up and is
  simply absent when it is not (see Tracing below), then:
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

- **Model calls are plain HTTP to Bedrock's OpenAI-compatible Mantle endpoint,
  authenticated with a bearer token — no boto3, no SigV4, no LangChain in the
  model path.** [ADR 0002](../adr/0002-bedrock-mantle-api-key.md) records why:
  classic `bedrock-runtime` Converse is `NOT_AUTHORIZED` account-wide, while
  Mantle answers the same models over an ordinary HTTPS call. LangGraph still
  owns orchestration; only the transport changed.
- `app/llm.py` is the whole transport: `chat()` and `chat_stream()`, plus the
  parsing helpers. Two things in it are load-bearing:
  - **The key is resolved per request** inside `_headers()`, never captured at
    import or baked into the client, so rotating `AWS_BEARER_TOKEN_BEDROCK`
    needs no cold start and a key missing at import does not poison the
    instance. A missing key raises, and the caller's fail-open policy turns
    that into a visibly degraded turn rather than a crash.
  - **`_client()` wraps an `lru_cache`d `AsyncClient`** — one connection pool
    per instance instead of a TLS handshake per judge, and one
    `monkeypatch.setattr` for tests. Unit tests must always replace it; they
    may never reach the live endpoint.
- **Read the end of a response, not the start.** Several models in the roster
  reason before answering: `nvidia.nemotron-nano-9b-v2` emits a
  `<think>…</think>` monologue into `content` and only then its verdict.
  `strip_reasoning()` removes closed blocks and discards an *unclosed* one
  entirely — an unclosed block means `finish_reason: length` cut the model off
  mid-thought, so there is no verdict and mining the fragment for one would
  turn a hypothetical into a decision. `_label()` then takes the **last**
  match, because prose reasoning ("this could pass, but … so fail") inverts
  under a first-match parse. `extract_content()` falls back to the `reasoning`
  field when `content` is null, which some models require.
- **Judge token budgets are generous, not tiny** (`JUDGE_MAX_TOKENS`, default
  512). A reasoning model truncated mid-monologue never reaches its verdict —
  the same failure family as the `gpt-oss-safeguard` models, which emit their
  verdict only inside a truncating `reasoning` field and are deliberately not
  in the roster. The ceiling is free on a terse model, because temperature 0
  stops it as soon as it has said the word.
- **Model ids are checked before an image is pushed** — `scripts/assert_models.py`,
  wired into `deploy.yml` ahead of the build. It lists `GET /v1/models` *and*
  spends a real one-token completion per id, because listing is not
  entitlement: several Claude ids appear in the catalogue on this account and
  still refuse to run. Every model step fails open, so a wrong or unentitled id
  ships as a working-looking chat with amber steps rather than as a crash — the
  assertion is what keeps that from being discovered by a visitor.
- Judges answer with a label and are parsed tolerantly (case, whitespace,
  punctuation, markdown, reasoning preamble). A response that is *not* a
  verdict degrades — it is never guessed at. The topic classifier's fallback
  chain is walked on model **errors only**: a model that answered is a model
  that is up, and re-asking costs a slice of the 55s turn budget for a question
  the output guard already backstops.
- `guard_output` is two independent halves. `scrub_failure()` is deterministic
  (URL allowlist — cadreai.com only — plus PII patterns), runs first, and has
  no outage mode; the guard model runs second and may degrade. A guard outage
  must never be able to leak an external URL.
- `app/persona.py` is the vetted baseline and the only source of facts until
  retrieval lands in Phase 3. Nothing else may state a fact about Cadre AI —
  no prices, no named clients, no invented capabilities — and the prompt says
  so explicitly rather than leaving it implied. `config.py` re-exports its
  `GREETING`/`SUGGESTIONS`/`CONTACT_URL` rather than restating them, so the
  copy `/config` advertises and the persona that must answer for it cannot
  drift. The dependency runs one way: persona never imports config.
- **Prompts live in `app/prompts/*.txt`, never in source code.** Every prompt
  — judge instructions, the brain's system prompt, the topic scope — is a
  plain text file read once at import via `_load()`. Placeholders use
  `{name}` syntax with `str.format()`. A new prompt means a new `.txt` file
  and a `_load("name", **kwargs)` call at module level — never an inline
  string, never an f-string buried in a function. This keeps the prompt
  text reviewable without reading Python and forces every wording change
  through a file whose diff shows exactly the prompt and nothing else.
  `app/persona.py` and `app/graph/models.py` own the `_load()` helpers and
  are the only places that import from the prompts directory.
- `config.BRAIN_MAX_TOKENS` bounds generation so the turn fits CloudFront's 60s
  origin cap (KB-004) *alongside four judge calls*; the persona asks for the
  same brevity, so answers end rather than get truncated. Judge latency is part
  of that budget — measure it before swapping a slot to a slower model.

### Tracing (`app/tracing.py`)

- `app/tracing.py` is the whole surface: `start_trace(client_id)` and
  `finalize_trace(...)`. Nothing else in the backend imports Langfuse.
- Every graph invocation emits a Langfuse trace, attached as Langfuse's
  LangChain `CallbackHandler` on the graph run — tracing rides the callback
  surface the orchestration already has, not bespoke logging per call site. The
  handler is a per-request object, so it goes on the invocation's `config`
  (`config["callbacks"]`), never on `ConversationState` (KB-008), exactly like
  `emit` goes on `config["configurable"]`. Because the model path is plain
  `httpx` (ADR 0002, no LangChain), the handler captures **graph-level node
  spans**, not individual Bedrock generations. That is the accepted shape;
  don't instrument `app/llm.py` to work around it.
- Langfuse credentials are three SSM parameters that **already exist with real
  values**, so Terraform only `data`-references them and injects
  `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` as function
  env vars (`infra/langfuse.tf`, which explains why ADR 0001 decision 4's
  create-with-placeholder pattern is the wrong one here). They are read **once
  at module import** — never per request, because a credential round trip
  inside a turn spends part of the 60s budget (KB-004) on something that cannot
  change between requests.
- A trace MUST carry at minimum: the `client_id` (as the Langfuse session id,
  so a visitor's turns group), which step refused if any, and per-step + total
  `latency_ms` — a refusal you can't attribute to a step is undebuggable, and
  debuggable refusals are the product. The per-step numbers are the `elapsed_ms`
  values already on the `state` wire events, reused rather than re-measured, so
  the trace and the stepper cannot disagree.
- The trace is marked publicly viewable and its URL goes out as the `trace` SSE
  event — the first frame of the response, since the id is generated locally and
  is known before the graph starts.
- **`finalize_trace` flushes twice, on purpose.** The graph's spans and the span
  carrying the trace-level fields both write trace attributes; in a single
  export batch the LangChain root span wins the upsert and `public`/`session_id`
  are silently dropped (verified against Langfuse Cloud). The first flush is
  what makes ours the later write — it is not redundant, don't delete it.
- Flush before `done` *and* before `error`: Langfuse batches events in a
  background thread and Lambda freezes the instance the moment the response
  ends, so an unflushed batch is a silently dropped trace.
- Tracing is fail-open — a Langfuse outage degrades observability, never the
  turn, the same posture as a degraded step verdict. Fail-open only counts while
  it stays visible (KB-009): every disabled or failed path logs a warning naming
  the cause, and the client learns about it from the *absence* of a `trace`
  event, never a broken one.

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
  `langgraph` and `langchain-core` came in with the engine, `httpx` with the
  model steps, and `langfuse` came in with tracing — dragging `langchain` with
  it, whose only job is to satisfy the bare `import langchain` guarding
  `langfuse.langchain`'s import path (see the comment in `requirements.txt`).
  There is deliberately no boto3: since ADR 0002 nothing in the request path
  signs anything.
- The image copies `app/` only. `scripts/` runs in CI and on a laptop against
  a real account; shipping it into the runtime would be cold-start weight for
  code no invoke ever executes.
- `AWS_BEARER_TOKEN_BEDROCK` is a *runtime* environment variable — Terraform
  sets it on the function from SSM, and local runs pass it with `docker run
  -e`. It must never be baked into an image layer, committed, or echoed.

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
