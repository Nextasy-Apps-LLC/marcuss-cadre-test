# cadre

`cadre` is a guardrailed streaming chatbot, live at
[cadre.marcuss.pro](https://cadre.marcuss.pro). A React page and a
`POST /ask` endpoint that streams Server-Sent Events — rail verdicts first,
then answer tokens, then a terminal `done` event — from a FastAPI backend
running as an arm64 container on Lambda.

This site is the reference documentation. It is built from the repository, so
the pages under **Infrastructure** and **Decisions** are the same Markdown that
lives in `infra/` and `adr/`, not a second copy of it.

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
                │ SigV4 (execution role)
          ┌─────▼─────┐
          │  Bedrock  │
          └───────────┘
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

## The SSE contract

`backend/app/sse.py` is the wire format, and `web/src/types.ts` mirrors it
verbatim. Four event types:

| Event | Payload |
|---|---|
| `rail` | `rail_id`, `rail_name`, `passed`, `latency_ms`, `reason`, `degraded` |
| `token` | `text` |
| `done` | `refused`, `refusal_reason`, `latency_ms` |
| `error` | `message` |

Six rails run in order, and the page renders all six as pending up front so a
stream that dies mid-turn shows *which* rail never reported rather than
spinning forever:

| Rail | Name | Role |
|---|---|---|
| `rail1` | `input_validation` | Shape, length, control characters |
| `rail2` | `injection` | Prompt-injection screen |
| `rail3` | `topic` | On-topic judge |
| `rail4` | `brain` | The answer itself |
| `rail5` | `output_guard` | Output-side guard |
| `rail6` | `scrub` | Final redaction pass |

A rail verdict carries `degraded` separately from `passed`. When a rail's model
call fails, the fail-open policy returns a pass — but the client renders that
amber, never green. An outage that reads as success is worse than a visible
outage.

!!! note "The backend is a walking skeleton"
    `backend/app/main.py` currently answers `ping` with `pong` and stubs
    everything else, while implementing the SSE contract in full. That was
    deliberate: it proves the React client, the CloudFront streaming path and
    the deploy pipeline end to end before any model is wired in. Replacing
    `_reply_for()` with real inference is then a change to one function rather
    than to the whole shape.

## Zero secrets

There is no static credential anywhere in this stack, which is why the
repository can be public without secret-scanning anxiety:

| Hop | How it authenticates |
|---|---|
| Lambda → Bedrock | SigV4 from the execution role. No API key exists to leak. |
| GitHub Actions → AWS | OIDC only. No `AWS_ACCESS_KEY_ID` in repository secrets. |
| CloudFront → Lambda URL | Lambda-typed OAC, SigV4-signed. |
| CloudFront → S3 | S3-typed OAC. The bucket blocks all public access. |

`terraform.tfvars` and `backend.hcl` are gitignored because an account id and a
state bucket name are pointless things to publish, not because they are
sensitive. The pattern for the first real secret — most likely an observability
key — is pre-agreed rather than improvised: an SSM `SecureString` seeded with a
placeholder and `lifecycle { ignore_changes = [value] }`, written out of band.

## Repository layout

```
backend/     FastAPI app + Dockerfile (arm64, Lambda Web Adapter)
web/         React + Vite single page, Vitest unit tests
infra/       Terraform — CloudFront, Lambda, S3, ACM, OIDC roles
adr/         Architecture decision records (MADR format)
docs/        This site
```

## Where to go next

- **[Infrastructure](infrastructure.md)** — the operational doc: first apply,
  attaching the custom domain, the streaming-breakers checklist, cost.
- **[CI and deployment](ci-cd.md)** — what the three workflows do and why
  shipping is manual.
- **[Decisions](adr/index.md)** — why the stack is shaped this way, including
  the traps that cost real time.
