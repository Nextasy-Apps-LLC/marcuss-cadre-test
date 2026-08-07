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
| topic classifier | **Nemotron 3 Nano 12B v2** (fallback: Haiku 4.5) | 3-way route: `in_scope` / `off_topic` / `needs_human` |
| query condensing | **Claude Haiku 4.5** | rewrite follow-ups into standalone retrieval queries |
| brain | **Claude Opus 5** (streaming) | answer quality on nuanced consulting questions |
| output safety | **Claude Haiku 4.5** guard + deterministic PII/URL scrub | final gate on the streamed answer |
| embeddings | **OpenAI `text-embedding-3-small`** | KB requirement; strong quality/cost for a small corpus |

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
1. **Query condensing** (multi-turn): with non-empty history, Haiku 4.5 rewrites the message into a standalone query ("how much does *that* cost?" → "Cadre AI Maturity Index pricing").
2. Embed the query with OpenAI `text-embedding-3-small`.
3. Vector search top-k≈6 with a similarity floor; hits (chunk text + source title/URL) are injected into the brain prompt with an instruction to cite sources inline.
4. **Fail-open**: an embeddings/DB outage degrades to persona-baseline answers; the `state` event reports `retrieve: skipped`, never a user-facing error.

### KB infrastructure — embedded LanceDB, not a database server

The corpus is ~50 pages / a few hundred chunks. The right-sized store is **LanceDB**: a real vector DB, but serverless/embedded — a columnar Lance dataset opened in-process by the Lambda. The ingestion output `backend/app/kb/cadre_kb.lance/` is committed and baked into the Docker image; queries are millisecond-local with zero cold-start dependency and $0 infra.

**Postgres + pgvector was considered and deliberately deferred**: it needs an always-on RDS/Aurora instance and a VPC-attached Lambda (NAT cost, slower cold starts) to serve a corpus that fits in memory. It becomes the right answer when the corpus grows large, updates frequently, or goes multi-tenant — listed under "with more time". If LanceDB misbehaves inside Lambda, **sqlite-vec** is the drop-in fallback (same embedded shape).

### Ingestion pipeline

`backend/ingest/` runs locally (never in Lambda): crawl allowlisted `www.cadreai.com` pages — home, about, the 4 service pages, industries (×8), departments (×8), contact, case-studies, events — plus all ~27 `/articles/*`; extract main content (beautifulsoup4), chunk ~800 tokens with heading/URL metadata, embed with OpenAI, write the LanceDB dataset. Refresh = re-run the script; automated re-ingestion is an explicit non-goal.

## Frontend — real-time state UI

- **Pipeline stepper**: live per-step chips (pending → running → pass/fail/skipped) driven by `state` events — the guardrail pipeline is visible, not implied.
- Streaming transcript; on refusal the client replaces the streamed buffer using `refusal_text`.
- Safe URL linkification (text→anchor for `https://` matches only; no `dangerouslySetInnerHTML`) so citations and the booking link are clickable.
- "View trace ↗" chip on the latest assistant message.
- Suggestion chips + greeting covering the assignment's support scenarios.

## Persona (brain node)

Cadre AI support assistant for prospective and existing clients: services (AI Strategy, AI Leadership & Facilitation, AI Engineering, AI Agents), industries served, the AI Maturity Index, the client portal, and how to get started. Facts come only from retrieved context and the vetted baseline — never invented pricing, clients, or capabilities. Pricing → engagements are custom; book a strategy call. Unknown or out-of-scope → escalate to https://www.cadreai.com/contact. Replies in the user's language.

## Phases

- [ ] **Phase 1 — LangGraph engine + SSE v2**: `StateGraph`, nodes with mocked-seam unit tests, `state` events, refusal/escalation states, persona v1, frontend protocol update + pipeline stepper.
- [ ] **Phase 2 — Langfuse traceability**: keys via SSM SecureString (`SET_OUT_OF_BAND` pattern), callback wiring, public traces, `trace` event, frontend trace link, flush hardening for Lambda.
- [ ] **Phase 3 — RAG KB**: ingestion pipeline + LanceDB artifact; `retrieve` node (condense → embed → search) wired between `topic_classifier` and `brain`; inline citations; OpenAI key via SSM; IAM/env deltas.
- [ ] **Phase 4 — UX polish + e2e**: stepper/linkify/suggestions polish; `BASE_URL`-pointable e2e suite (healthz, config, grounded-answer-with-citation, off-topic refusal, injection refusal) run against local Docker, then prod.
- [ ] **Phase 5 — Hardening + live verification**: the assignment's support scenarios exercised live in a browser, including escalation and trace-link click-through.

## Scope decisions

**In scope:** the assignment's support scenarios; grounded, cited answers; the five-step guarded pipeline with live visible state; public per-message traces; escalation to a human via booking.

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

## Operational notes

- Secrets (Langfuse public/secret keys, OpenAI key) live as SSM SecureString parameters created by Terraform with `value = "SET_OUT_OF_BAND"` + `ignore_changes`; real values are set manually once via `aws ssm put-parameter`.
- Bedrock model availability (Nemotron 3 Nano, model ids) is asserted at the start of Phase 1; every fallback is named in the roster above.
- Deploy path is unchanged from the POC: push to `main` → CI → Terraform + image build → CloudFront. Every phase merges deployable.
