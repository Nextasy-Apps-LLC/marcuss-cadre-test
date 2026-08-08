# plan.md — Cadre AI support chatbot

A customer-support chatbot for **Cadre AI** (AI strategy & implementation consultancy), live at **https://cadre.marcuss.pro**.

**Stack:** React/Vite SSE client → CloudFront → Lambda Function URL (response streaming) → FastAPI + **LangGraph** conversation engine → AWS Bedrock (Claude Opus 5, Claude Haiku 4.5, NVIDIA Nemotron 3 Nano) + OpenAI embeddings → **Langfuse** for end-to-end tracing with public trace links.

**This file is the epic.** Each phase below is broken down into GitHub issues (via `/compound-create-issue`) that carry the implementation detail; the issues reference their phase here. Process, board, and knowledge-base mechanics: see `CLAUDE.md`.

## Foundations

Before this plan was written, a POC walking skeleton was built and deployed end-to-end to de-risk the delivery path: CloudFront ↔ Lambda Function URL ↔ SSE streaming (the traps found are recorded in ADR 0001), private S3 + OAC for the static client, CI/CD with Terraform, and a smoke-tested `POST /ask` pipeline. Alongside it, a minimal compound-engineering setup (`.claude/` skills, kanban board, `kb/learnings.json`, ADRs) was created so that every phase below compounds on recorded learnings. That scoping work directs this plan; everything from here is forward-only.

## Architecture — LangGraph conversation engine

The backend is orchestrated as a **LangGraph `StateGraph`** over a typed `ConversationState` (`message`, `history`, `client_id`, `steps[]`, `context`, `answer`, `outcome ∈ {answered, refused, escalated, error}`, `refusal_text`, `trace_url`). Every terminal is an explicit state; every transition is observable (SSE + Langfuse).

```
validate_input ──fail──▶ refuse
      │ pass
injection_check ──fail──▶ refuse
      │ pass
topic_classifier ──off_topic──▶ refuse
      │ in_scope         └──needs_human──▶ escalate (book a call → cadreai.com/contact)
retrieve   ← KB lookup: condense query → embed → LanceDB top-k (fail-open)
      │
brain      ← streams tokens live, cites retrieved sources inline
      │
output_safety ──fail──▶ retract → refuse
      │ pass
done (answered)
```

### Model roster — a fit-for-purpose model per step

All chat models via `langchain-aws` (`ChatBedrockConverse`); availability in us-east-1 is verified at implementation time, fallbacks noted.

| Step | Model | Why |
|---|---|---|
| input validation | deterministic checks + **Nemotron 3 Nano 9B v2** (Bedrock serverless) | cheap SLM sanity/validity judge |
| injection check | **Claude Haiku 4.5** strict single-token classifier | fast, strong instruction-following against adversarial input; Bedrock Guardrails prompt-attack filter evaluated as a complement |
| topic classifier | **Nemotron 3 Nano 12B v2** (fallbacks: **Google Gemma** on Bedrock, then Haiku 4.5) | 3-way route: `in_scope` / `off_topic` / `needs_human` |
| query condensing | **Google Gemma 3 12B** (`CADRE_MODEL_CONDENSE`) | rewrite follow-ups into standalone retrieval queries. plan.md specified Haiku 4.5; no Claude id answers through Mantle (ADR 0002), so the slot took the fastest entitled model. Probed before pinning: 10/10 usable rewrites, p50 0.39s, and condensing lifts the mean top-hit score on 5 real follow-ups from 0.250 (raw message) to 0.602 — "how much does that cost?" alone retrieves nothing at all |
| brain | **Claude Opus 5** (streaming) | answer quality on nuanced consulting questions |
| output safety | **Claude Haiku 4.5** guard + deterministic PII/URL scrub | final gate on the streamed answer |
| embeddings | **OpenAI `text-embedding-3-large`** (3072 native) | KB requirement; on a corpus this small the cost difference is noise and retrieval quality is the only axis that matters (decision recorded in #62, superseding `-3-small`) |

## SSE protocol v2 — real-time through every phase

The FastAPI endpoint bridges the graph to SSE via an asyncio queue; node wrappers emit events as the graph advances, so the client renders the pipeline live:

- `state` — `{step, status: running | pass | fail | skipped, detail?}` on **every** transition (the "current conversation state" event)
- `token` — brain deltas, streamed as generated
- `trace` — `{trace_id, url}` as soon as the Langfuse trace exists
- `done` — `{outcome, refusal_text?}` · `error` · `ping` heartbeat (idle-timeout safety)

Stream-then-retract is explicit: tokens stream during `brain`; a later `output_safety` fail instructs the client to replace the streamed buffer with `refusal_text`, this is non ideal for a production grade chatbot but it is ideal for this submission since it shows the whole process.

## Traceability — Langfuse

- Langfuse Cloud; the SDK's LangChain `CallbackHandler` is attached to every graph invocation, so each node and LLM call lands as a span/generation on one trace.
- A deterministic trace id per request lets the backend emit the trace URL in the `trace` SSE event immediately; the trace is marked `public=True`, so the link opens without a login.
- The web client renders a **"View trace ↗"** chip on the latest assistant message.
- `langfuse.flush()` runs in a `finally` before the response closes — Lambda freezes after responding, and unflushed events are lost.
- Trade-off, accepted for a demo: public traces expose user messages. Called out under scope.

## Knowledge base (RAG) — when and how the bot consults it

**The KB is consulted on every in-scope user message, as a first-class LangGraph node (`retrieve`)** — the standard LangGraph RAG pattern (retrieval as a graph step, not a hidden call inside the brain). It runs **after `topic_classifier` passes and before `brain`**: refused/escalated messages never spend an embedding call, and the brain always has its context before generating. As a node it gets its own SSE `state` events (`retrieve: running → pass/skipped`) and its own Langfuse span (query, top-k hits, scores — all visible in the public trace).

Inside `retrieve`:
1. **Query condensing** (multi-turn): with non-empty history, Gemma 3 12B rewrites the message into a standalone query ("how much does *that* cost?" → "Cadre AI Maturity Index pricing"). With empty history the call is skipped entirely — a first message is already standalone, and confirming that would spend part of the 60s turn budget on a no-op.
2. Embed the query with OpenAI `text-embedding-3-large` — the same model at the same 3072 dimensions the artifact was built with. A mismatch does not raise, it returns wrong neighbours, so `retrieve` checks the query vector against `app/kb/manifest.json` before it searches.
3. Vector search top-k≈6 with a similarity floor; hits (chunk text + source title/URL) are injected into the brain prompt with an instruction to cite sources inline as a simple small "see more" link at the end that when clicked takes you the original http, it should not show the long url.
4. **Fail-open**: an embeddings/DB outage degrades to persona-baseline answers; the `state` event reports `retrieve: skipped`, never a user-facing error. Each way of giving up is its own machine-readable `detail` — `kb_unavailable`, `kb_disabled`, `kb_dimension_mismatch`, `kb_timeout` — because "the KB is off" and "the KB is answering from the wrong corpus" need different people woken up. Zero hits is a `pass`/`no_hits`, not a skip: the KB ran, it had nothing.

### KB infrastructure — embedded LanceDB, not a database server

The corpus is ~50 pages / a few hundred chunks. The right-sized store is **LanceDB**: a real vector DB, but serverless/embedded — a columnar Lance dataset opened in-process by the Lambda. The ingestion output `backend/app/kb/cadre_kb.lance/` is committed and baked into the Docker image; queries are millisecond-local with zero cold-start dependency and $0 infra.

**Postgres + pgvector was considered and deliberately deferred**: it needs an always-on RDS/Aurora instance and a VPC-attached Lambda (NAT cost, slower cold starts) to serve a corpus that fits in memory. It becomes the right answer when the corpus grows large, updates frequently, or goes multi-tenant — listed under "with more time". If LanceDB misbehaves inside Lambda, **sqlite-vec** is the drop-in fallback (same embedded shape).

### Ingestion pipeline

`backend/ingest/` runs locally (never in Lambda): fetch a **frozen 55-URL allowlist** of `www.cadreai.com` pages — home, about, contact, case-studies, events, the 4 service pages, industries (×9 — the sitemap has nine, not eight) and departments (×8), plus all 27 `/articles/*` — one request per second, honest User-Agent, `robots.txt` enforced, never following a link. Extract main content (beautifulsoup4 + lxml), drop the blocks every page shares (this site's footer is `<div>`s, so the tag list alone leaves the menu in the corpus), chunk ~800 tokens with ~100 overlap and heading/URL metadata, embed with `text-embedding-3-large`, write the LanceDB dataset and a manifest recording model, dimension and counts. Refresh = re-run the script and commit the artifact; automated re-ingestion is an explicit non-goal.

As built (2026-08-08): 55 pages → 131 chunks (median 755 tokens), a 1.80 MB committed artifact, 85K embedding tokens ≈ $0.011 per full rebuild. Details in `backend/ingest/README.md`.

## Frontend — real-time state UI

- **Pipeline stepper**: live per-step chips (pending → running → pass/fail/skipped) driven by `state` events — the guardrail pipeline is visible, not implied.
- Streaming transcript; on refusal the client replaces the streamed buffer using `refusal_text`.
- Safe URL linkification (text→anchor for `https://` matches only; no `dangerouslySetInnerHTML`) so citations and the booking link are clickable.
- "View trace ↗" chip on the latest assistant message.
- Suggestion chips + greeting covering the assignment's support scenarios.
- **Thumbs up / thumbs down on assistant messages — rendered as a no-op**: the buttons appear (with a brief "thanks for the feedback" acknowledgment) but are wired to nothing. They document the intended feedback loop; actually recording them as Langfuse scores on the message's trace is listed under "with more time".

## Persona (brain node)

Cadre AI support assistant for prospective and existing clients: services (AI Strategy, AI Leadership & Facilitation, AI Engineering, AI Agents), industries served, the AI Maturity Index, the client portal, and how to get started. Facts come only from retrieved context and the vetted baseline — never invented pricing, clients, or capabilities. Pricing → engagements are custom; book a strategy call. Unknown or out-of-scope → escalate to https://www.cadreai.com/contact. Replies in the user's language.

## Evaluation — LLM-as-judge

"Is the bot doing things correctly, and did this change make it better or worse?" is answered by an **offline eval harness** (`backend/evals/`) that runs the LangGraph engine headless against a golden dataset and logs every run as a **Langfuse experiment**, so run-vs-run comparison (per-case and aggregate score deltas) comes from Langfuse's native experiment UI — no dashboard to build.

- **Golden dataset (~30 cases, growable)**: the assignment's support scenarios plus adversarial ones — off-topic, injection attempts, multi-turn follow-ups, pricing, unknown-answer escalation. Each case: input (+history) → expected `outcome` (`answered/refused/escalated`), expected source URL for grounded questions, and a reference answer. Stored in-repo; mirrored to a Langfuse dataset.
- **Deterministic assertions first** (no LLM, exact): the injection case got `refused`; the unknown case `escalated` with the booking link; the grounded answer cites the expected page.
- **LLM-as-judge for the fuzzy half**: rubric grading of groundedness (only claims supported by the retrieved chunks — the hallucination check), correctness vs the reference answer, and persona/tone adherence. **Reference-guided grading** blunts self-preference bias, and the judge is a **different model family from the brain** — Nemotron 3 Super 120B (fallback: GPT-OSS), both serverless on Bedrock, so no new infra.
- **When it runs**: manually or via `workflow_dispatch` before merging any prompt, model, or retrieval change. A full run costs cents and a few minutes. CI *gating* on scores is deliberately deferred (see scope) — judge scores need calibration before they're allowed to block merges.

## Phases

- [x] **Phase 1 — LangGraph engine + SSE v2**: `StateGraph`, nodes with mocked-seam unit tests, `state` events, refusal/escalation states, persona v1, frontend protocol update + pipeline stepper. — shipped via #24/#25/#26/#27 (PRs #29, #31, #35)
- [ ] **Phase 2 — Langfuse traceability**: keys via SSM SecureString (`SET_OUT_OF_BAND` pattern), callback wiring, public traces, `trace` event, frontend trace link, flush hardening for Lambda.
- [ ] **Phase 3 — RAG KB**: ingestion pipeline + LanceDB artifact (shipped: 55 pages → 131 chunks, 1.80 MB); `retrieve` node (condense → embed → search) wired between `topic_classifier` and `brain`, fail-open with per-cause `detail`s and its own Langfuse span; cited answers via `persona.system_prompt(context)`; OpenAI key `data`-read from SSM into the function (`infra/openai.tf`). Remaining: the web half — linkify's KB-017 fix and the `/case-studies` / `/articles` label mappings.
- [ ] **Phase 4 — Evaluation harness**: golden dataset; headless graph runner; deterministic outcome/citation assertions; LLM-judge rubric (groundedness, correctness, persona) on a non-brain model family; runs logged as Langfuse experiments for before/after comparison.
- [ ] **Phase 5 — UX polish + e2e**: stepper/linkify/suggestions polish; no-op thumbs up/down; `BASE_URL`-pointable e2e suite (healthz, config, grounded-answer-with-citation, off-topic refusal, injection refusal) run against local Docker, then prod.
- [ ] **Phase 6 — Hardening + live verification**: the assignment's support scenarios exercised live in a browser, including escalation and trace-link click-through; a final eval run recorded as the submission baseline.

## Scope decisions

**In scope:** the assignment's support scenarios; grounded, cited answers; the five-step guarded pipeline with live visible state; public per-message traces; escalation to a human via booking; an offline LLM-as-judge eval harness with run-vs-run comparison.

**Out of scope** (each deliberate, with the "with more time" path):

| Deferred | Why now | With more time |
|---|---|---|
| Auth / user accounts | demo is anonymous | Cognito in front of CloudFront |
| CRM/ticket handoff on escalation | no CRM to integrate against | webhook to HubSpot/Zendesk from the `escalate` node |
| Conversation persistence | in-request history covers the demo | DynamoDB session store keyed by client id |
| Analytics dashboard | Langfuse already captures per-trace data | Langfuse dashboards / metrics API |
| Distributed rate limiting | single-Lambda in-process limiter suffices | DynamoDB token bucket |
| Automated KB re-ingestion | corpus changes rarely | scheduled ingestion + freshness checks |
| pgvector/RDS-backed KB | corpus fits in an embedded store | migrate when corpus scale/tenancy demands it |
| Langfuse self-hosting | Cloud free tier fits a demo | self-host if data residency requires |
| Public traces privacy | acceptable for a demo | per-conversation opt-in, redaction in traces |
| User feedback wiring | thumbs up/down ship as a no-op UI | record as Langfuse scores on the message's trace; feed the eval dataset |
| Online / continuous evaluation | offline harness covers the demo | scheduled LLM-judge over sampled production traces; drift alerts |
| CI gating on eval scores | judge needs calibration first | block merges on score regression once judge↔human agreement is measured |

## Operational notes

- Secrets (Langfuse public/secret keys, OpenAI key) live as SSM SecureString parameters created by Terraform with `value = "SET_OUT_OF_BAND"` + `ignore_changes`; real values are set manually once via `aws ssm put-parameter`.
- Bedrock model availability (Nemotron 3 Nano, model ids) is asserted at the start of Phase 1; every fallback is named in the roster above.
- Deploy path is unchanged from the POC: push to `main` → CI → Terraform + image build → CloudFront. Every phase merges deployable.
