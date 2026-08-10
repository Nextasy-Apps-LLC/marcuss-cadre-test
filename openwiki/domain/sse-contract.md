---
type: API Contract
title: SSE contract v2 — steps, states, tokens
description: The cadre SSE v2 wire format — trace, state, token, done, error events plus the ping heartbeat; the six pipeline steps in order; status semantics (degraded, skipped, lost, stream-then-retract); the per-turn tokens/cost/latency aggregate on done.summary; the LangGraph backend and hand-rolled fetch-SSE client; and the tests that pin the contract.
tags: [sse, contract, steps, streaming, langgraph, fastapi, react]
---

# SSE contract v2

The wire format is defined in `backend/app/sse.py` and mirrored verbatim in
`web/src/types.ts`. Nothing imports across the language boundary, so renaming a
field compiles green on both sides and breaks silently in a browser (KB-005) —
change both sides in one PR, or neither. It streams over the
[architecture's path](/openwiki/architecture/overview.md).

## The events

Every frame is `event: <name>` + `data: <json>`; `: ping` comment frames (no
data) keep intermediaries from reaping a connection that is merely waiting on a
slow step, and the client drops them.

| Event | Payload | Meaning |
|---|---|---|
| `trace` | `trace_id`, `url` | The public Langfuse trace link — at most once, the first frame of the turn; absent entirely when tracing is down (fail-open). |
| `state` | `step`, `status` (`running`/`pass`/`fail`/`skipped`), `detail?`, `elapsed_ms?`, `retrieval?` | One pipeline transition; each of the six steps emits `running` then its verdict. `elapsed_ms` is an int on `pass`/`fail`, `null` otherwise; `retrieval` is non-`null` only on `retrieve`'s verdict. |
| `token` | `text` | A fragment of the answer, only while `brain` (or the escalation text) streams. |
| `done` | `outcome` (`answered`/`refused`/`escalated`/`error`), `refusal_text?`, `summary?` | Always the terminal event of a normal stream. `summary` is the per-turn aggregate `finalize_trace` computed (issue #109), present only when tracing ran. |
| `error` | `message` | Terminal on its own — no `done` follows. Generic on the wire, detailed in the log. |

`state` events are written *before* the step works (wire first, then state), so
the stepper is live rather than a replay printed at the end. Malformed bodies
are **not** HTTP errors: a body the graph cannot be handed gets
`validate_input` fail + `skipped` for the rest + `done {refused}` — still HTTP
200, so the browser uses its normal `done` path, not an offline branch.

The `retrieval` payload (`sse.retrieval`, issue #74) is `{query, hit_count,
top_score}`:

- `query` is the **condensed** query, and only when it differs from the
  visitor's message — `None` on a first message and on the KB-011 fallback to
  the visitor's own words.
- `hit_count`/`top_score` describe the **final** slate — after the
  `RETRIEVE_MIN_SCORE` floor, the per-URL dedupe and the `RETRIEVE_TOP_K` cut —
  because that is the context the brain actually read. `top_score` is `None`
  exactly when `hit_count` is 0.
- Deliberately no chunk text and no URLs: the passages are already in the
  brain's prompt, and duplicating them would make every frame expensive for no
  new fact.

`done`'s optional `summary` (issue #109) is what `tracing.finalize_trace`
returned: `latency_ms`, `tokens {input, output, total}`, `cost_usd`,
`usage_tokens`/`step_cost_usd` keyed by step, and `usage_source`/`cost_source`
(`"provider"`/`"model_prices"`/`"unpriced"`/`"absent"`). It appears only when
tracing ran and returned a payload — a missing field means "no aggregate",
never zeros (KB-009) — and the same dict is written to the Langfuse turn span,
so the transcript line, the stepper's Total row and the trace cannot disagree.

```mermaid
sequenceDiagram
  participant B as Browser
  participant CF as CloudFront
  participant L as Lambda FastAPI (LangGraph)
  B->>CF: POST /ask with x-amz-content-sha256
  CF->>L: SigV4-signed origin request via OAC
  L-->>B: state per step: running, then its verdict
  L-->>B: token chunks (8 chars each)
  L-->>B: done answered, refused, or escalated
  Note over B: done refusal_text replaces the streamed buffer —<br/>tokens are provisional (stream-then-retract)
```

## The six steps

`backend/app/sse.py` (`STEPS`) and `web/src/types.ts` (`STEPS`/`STEP_LABELS`)
must agree:

| # | id | Role |
|---|---|---|
| 1 | `validate_input` | Deterministic checks (empty, >2000 chars, control chars, client-id format, rate limit) then an SLM validity judge |
| 2 | `injection_check` | Prompt-injection classifier |
| 3 | `topic_classifier` | Three-way route: in-scope → `retrieve`; off-topic → refuse; `needs_human` → escalate |
| 4 | `retrieve` | Condense follow-ups → OpenAI embed (`text-embedding-3-large`) → LanceDB search; passes with `no_hits` when the slate is empty, else fills the brain's `context`; fails open to `skipped` (`kb_timeout`/`kb_disabled`/`kb_dimension_mismatch`/`kb_unavailable`) and the brain answers from the persona baseline |
| 5 | `brain` | The answer; streams tokens, and is the one step with no fail-open path — a failure propagates to a terminal `error` |
| 6 | `output_safety` | Guard on the complete streamed answer; fail → stream-then-retract refusal |

## Status and outcome semantics

- **`pass` with `detail: "degraded"`** — the verdict came from the fail-open
  policy (model errored or returned no verdict), not a real classification;
  rendered amber, never green (KB-009). v2 keys this off `detail`, not a wire
  status of its own.
- **`skipped` is server-authoritative** — `sse.unreported()` emits it for every
  step that never ran before a terminal refusal/escalation, so the client never
  infers anything from silence.
- **Client-only statuses** (`web/src/types.ts`): `pending` (painted up front),
  `lost` (still pending when the stream died without `done` — genuinely unknown).
- `done {refused}` can arrive *after* tokens: `refusal_text` overwrites the
  screen. `escalated` streams the booking text as tokens and keeps `status:
  done` with `outcome` so the UI renders it distinctly.

## Backend and client

- `backend/app/main.py` compiles the LangGraph `StateGraph` once at import;
  `/ask` runs it as a task and drains an `asyncio.Queue`. Nodes emit through
  `QueueEmitter`, which rides LangGraph `config["configurable"]` — never the
  checkpointable state channel (KB-008) — so the SSE stream is a projection of
  the graph's progress.
- Model steps are in `app/graph/models.py`; the transport is `app/llm.py`:
  plain httpx against Bedrock's Mantle endpoint with a bearer key (ADR 0002),
  bounded retry on 5xx only, and never a retry after the first token delta.
- Queries are embedded by `app/embeddings.py` — one plain-httpx POST to
  `api.openai.com` per in-scope turn (key read per request, so a rotation
  needs no cold start); `retrieve` bounds the whole node with
  `RETRIEVE_TIMEOUT_S`.
- In-process rate limiter (`app/ratelimit.py`): 30 turns / 60s per instance;
  a refusal arrives as `validate_input` fail with `rate_limited`.
- `web/src/lib/sse.ts` hand-rolls a fetch reader (`TextDecoder {stream: true}`
  for multi-byte chars, buffer drained at `\n\n`, comment frames dropped) —
  `EventSource` only issues GETs and can't set headers. Don't "simplify" back.
- Every `/ask` POST carries `x-amz-content-sha256` (`sha256Hex`) or it 403s
  (KB-002). Keep the composer's 2000-char cap equal to `config.MAX_INPUT_LEN`;
  greeting/chips come from `/config` so they can't drift.

## Tests that pin the contract

- `backend/tests/test_ask.py` — the malformed-body wire sequence, refusal
  shapes; `test_graph.py` — routing (fail → refuse, needs_human → escalate);
  `test_models.py` — verdict parsing incl. `_label`'s space/hyphen tolerance;
  `test_llm.py` — transport, retry, no-retry-after-first-delta;
  `test_retrieve_wire.py` — the `retrieval` payload on the wire;
  `test_kb_store.py` / `test_embeddings.py` — the store and the embed call;
  plus `test_ratelimit.py`, `test_persona.py`, `test_assert_models.py`;
  `test_tracing.py` / `test_tracing_phase1.py` — per-step + per-turn
  usage/cost summary, incl. interleaved turns.
- `web/src/lib/sse.test.ts` — `parseFrame`/`readSse`/`sha256Hex`;
  `web/src/lib/turnReducer.test.ts` — step transitions;
  `web/src/types.test.ts` — statuses, `freshSteps()`;
  `web/src/lib/usage.test.ts` — token/cost formatting (tiny costs written out
  in full, never scientific notation); `PipelineStepper.test.ts` /
  `Transcript.test.ts` — the per-step usage rows, Total row and reply-summary
  line rendered from `done.summary`.
- `backend/tests/e2e/` — real-target suite behind the `e2e` marker and the
  `CADRE_E2E_BEDROCK` gate ([CI/CD](/openwiki/workflows/ci-cd.md)); asserts no
  `Content-Length` (KB-010) and real incremental tokens.

Run with `pytest` (backend/) and `npm test` (web/);
[CI](/openwiki/workflows/ci-cd.md) runs the unit suites on every push and PR.
