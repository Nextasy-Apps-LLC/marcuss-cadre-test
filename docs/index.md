# cadre

`cadre` is a guardrailed streaming chatbot, live at
[cadre.marcuss.pro](https://cadre.marcuss.pro). A React page and a
`POST /ask` endpoint that streams Server-Sent Events — per-step pipeline
verdicts as they happen, then answer tokens, then a terminal `done` event —
from a FastAPI backend running a **LangGraph conversation engine** as an arm64
container on Lambda.

This site is the reference documentation, and it is split by audience. **For
humans** — the reviewer reading this cold — is everything from the review
walkthrough down through the decisions: what this is, how a turn flows, how to
run it, why it is shaped this way. **For agents** is the machine-facing
contract that keeps an AI coding assistant correct: the `CLAUDE.md` family and
the generated `openwiki/` knowledge base. Each side says so on its landing
page. Several pages are the *same Markdown* that lives at the repository root
(`plan.md`, `adr/`, `infra/README.md`, the `CLAUDE.md` files), embedded at
build time — one source of truth per fact, not a second copy.

Reviewing the submission? Start with the [review walkthrough](review.md).

## The shape

One CloudFront distribution, one hostname, two origins:

- a **private S3 bucket** serving the built page as the default cache
  behavior, with `Managed-CachingOptimized` and compression on; and
- a **Lambda Function URL** in `RESPONSE_STREAM` invoke mode serving
  `/ask`, `/healthz` and `/config` as ordered cache behaviors, with caching
  and compression deliberately **off**.

Because both origins answer under `cadre.marcuss.pro`, the browser's fetch to
`/ask` is same-origin with the page that issued it — no CORS preflight sits in
front of the SSE connection. The backend still configures CORS, but only for
the local Vite dev server; production traffic never exercises it.

The Function URL is `AWS_IAM`, never anonymous. CloudFront signs every origin
request with SigV4 through a Lambda-typed Origin Access Control, and a second
S3-typed OAC does the equivalent job for the bucket. This is not optional
hardening: the AWS account carries an org-level data perimeter that rejects
`NONE`-auth Function URLs outright.

``` text
                cadre.marcuss.pro
                       │
               ┌───────▼────────┐
               │   CloudFront   │
               └───┬────────┬───┘
      /ask         │        │      everything else
      /healthz     │        │
      /config      │        │
          ┌────────▼──┐  ┌──▼──────────┐
          │ Lambda    │  │ S3 (private)│
          │ Fn URL    │  │  OAC        │
          │ AWS_IAM   │  └─────────────┘
          │ STREAM    │
          └─────┬─────┘
                │ bearer token (ADR 0002)
          ┌─────▼─────────────┐
          │ Bedrock (Mantle)  │
          └───────────────────┘
```

## Why not API Gateway

Streaming is the whole point, and most of the natural path buffers. API Gateway
HTTP APIs buffer the full response before returning it; so does any non-zero-TTL
CloudFront cache policy; so does response compression. Each one silently turns
"streaming" into "a long pause, then everything at once" — no error anywhere in
the chain.

So there is no API Gateway in this stack. The Function URL is invoked directly,
fronted only by CloudFront, and the container runs the
[AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) as
an extension with `AWS_LWA_INVOKE_MODE=response_stream`. The adapter turns a
Lambda invoke into an ordinary HTTP request against uvicorn on `:8080`, so the
same image runs unchanged under `docker run -p 8080:8080` locally and inside
Lambda in production.

The full list of settings that will quietly break streaming is in
[Infrastructure](infrastructure.md#things-that-will-silently-break-streaming),
and the reasoning behind each is in
[ADR 0001](adr/0001-streaming-chatbot-cloudfront-lambda-s3.md).

## The pipeline and the SSE contract (protocol v2)

The backend is a LangGraph `StateGraph`: every step of the guarded pipeline is
a node, every terminal (`answered` / `refused` / `escalated` / `error`) is an
explicit state, and the SSE stream is a live projection of the graph's
progress. `backend/app/sse.py` is the wire format's single source of truth, and
`web/src/types.ts` mirrors it verbatim. Five event types plus a `: ping`
comment heartbeat:

| Event | Payload |
|---|---|
| `trace` | `trace_id`, `url` — the public Langfuse trace link, sent as the **first** frame of the turn; absent entirely when tracing is down (fail-open) |
| `state` | `step`, `status` (`running` \| `pass` \| `fail` \| `skipped`), `detail?`, `elapsed_ms` (int on `pass`/`fail`, `null` otherwise — the same numbers the trace reports), `retrieval` (`{query, hit_count, top_score}` on `retrieve`'s terminal `pass`, `null` everywhere else — the same facts the trace's retrieval span records, minus the URLs) |
| `token` | `text` |
| `done` | `outcome` (`answered` \| `refused` \| `escalated` \| `error`), `refusal_text?` |
| `error` | `message` |

Six steps run in order, and the page paints one stepper chip per step up front:

| Step | Role |
|---|---|
| `validate_input` | Deterministic checks (rate limit, id shape, length, control characters) + an SLM validity judge |
| `injection_check` | Prompt-injection screen |
| `topic_classifier` | Three-way route: `in_scope` / `off_topic` / `needs_human` (escalation) |
| `retrieve` | KB lookup: condense the query (follow-ups only) → embed → search the committed LanceDB corpus, top-k with a similarity floor. Fail-open: an outage, an absent artifact or a manifest mismatch reports `skipped` with the reason and the turn answers from the vetted baseline |
| `brain` | The answer itself, streamed token by token |
| `output_safety` | Deterministic URL/PII scrub + a guard model on the complete reply |

A failing check routes to a `refuse` terminal; `needs_human` routes to
`escalate` with a booking link. Skips are **server-authoritative**: on a
terminal refusal the server emits `state {status: "skipped"}` for every step
that never reported, so the client never guesses what silence means. A `pass`
whose `detail` is `degraded` came from the fail-open policy — a model outage
degrades observability, never a visitor's turn — and renders amber, never
green. The full contract semantics live next to the code in
[`backend/CLAUDE.md`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/backend/CLAUDE.md).

## Secrets: a handful, all SSM-held, none in GitHub

ADR 0001 designed a zero-secret stack; [ADR 0002](adr/0002-bedrock-mantle-api-key.md)
knowingly retracted one row of it when classic `bedrock-runtime` turned out to
be `NOT_AUTHORIZED` account-wide — model calls now go over Bedrock's
OpenAI-compatible Mantle endpoint with a Bedrock API key as a bearer token.

| Hop | How it authenticates |
|---|---|
| Lambda → Bedrock | Bearer token from an SSM `SecureString` (`/cadre/bedrock-api-key`), injected as `AWS_BEARER_TOKEN_BEDROCK`. |
| Lambda → OpenAI (embeddings) | API key from an SSM `SecureString`, `data`-read by Terraform into the environment. |
| Lambda → Langfuse (tracing) | Secret/public key pair, same SSM pattern. Fail-open: tracing down never breaks a turn. |
| GitHub Actions → AWS | OIDC only. No `AWS_ACCESS_KEY_ID` in repository secrets. |
| CloudFront → Lambda URL | Lambda-typed OAC, SigV4-signed. |
| CloudFront → S3 | S3-typed OAC. The bucket blocks all public access. |

The authoritative secrets table — every parameter name, who reads it, who may
write it — lives in
[infra/README.md §Secrets and credentials](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/infra/README.md#secrets-and-credentials).
`terraform.tfvars` and `backend.hcl` are gitignored because an account id and a
state bucket name are pointless things to publish, not because they are
sensitive.

## Repository layout

```
backend/       FastAPI + LangGraph engine, Dockerfile (arm64, Lambda Web Adapter)
backend/evals/ Judge benchmark harness — picks the judge models on numbers
web/           React + Vite page with the live pipeline stepper, Vitest tests
infra/         Terraform — CloudFront, Lambda, S3, ACM, OIDC roles
adr/           Architecture decision records (MADR format)
kb/            learnings.json — the compounding knowledge base
.claude/       Compound workflow: skills, agents, kanban recipe
openwiki/      Generated agent knowledge base (daily workflow; do not hand-edit)
docs/          This site
```

[`plan.md`](plan-epic.md) at the repository root is the epic: architecture,
model roster, phases, and the scope-decision table.

## Where to go next

- **[Review walkthrough](review.md)** — the demo script and the
  dimension-by-dimension evidence map for reviewers.
- **[The Claude Code workflow](claude-code.md)** — the compound loop, the
  skills and subagents, the KB, and the honesty scanner: 30% of the grade,
  one page.
- **[Infrastructure](infrastructure.md)** — the operational doc: first apply,
  attaching the custom domain, the streaming-breakers checklist, cost.
- **[CI and deployment](ci-cd.md)** — what the workflows do and why
  shipping is manual.
- **[Decisions](adr/index.md)** — why the stack is shaped this way, including
  the traps that cost real time.
- **[For agents](agents/index.md)** — the machine-facing half: the `CLAUDE.md`
  family, `openwiki/`, and how an AI assistant is kept correct.
