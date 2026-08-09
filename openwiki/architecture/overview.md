---
type: Architecture Overview
title: Streaming chatbot architecture
description: How cadre streams SSE end-to-end — one CloudFront distribution with a private S3 page origin and an AWS_IAM RESPONSE_STREAM Lambda Function URL origin, the four silent streaming-breakers, the SSM-held secrets (Bedrock + OpenAI + Langfuse), and the ADR 0001/0002/0003 decisions.
tags: [architecture, streaming, cloudfront, lambda, sse]
---

# Architecture — one distribution, two origins, one hostname

Everything is served from one CloudFront distribution, so `fetch("/ask")` is
same-origin with the page — no CORS preflight in front of the SSE connection.
The shape is recorded in `adr/0001-streaming-chatbot-cloudfront-lambda-s3.md`;
don't fight it without a superseding ADR — and note `adr/0002` now supersedes
ADR 0001's Bedrock-authentication statements.

```mermaid
flowchart LR
  B["Browser at cadre.marcuss.pro"] --> CF["CloudFront<br/>http_version = http2"]
  CF -->|"default behavior<br/>CachingOptimized, compress"| S3[("Private S3<br/>React page")]
  CF -->|"/ask /healthz /config<br/>CachingDisabled, no compress"| FU["Lambda Function URL<br/>AWS_IAM, RESPONSE_STREAM"]
  FU --> LWA["Lambda Web Adapter<br/>uvicorn on port 8080"]
  LWA -->|"httpx, bearer token<br/>(ADR 0002)"| BR["Bedrock Mantle<br/>OpenAI-compatible"]
```

## Why there is no API Gateway

API Gateway HTTP APIs, any non-zero-TTL cache policy, and compression each
buffer the full response — streaming silently becomes one blob, no error
anywhere. Instead: a Function URL with `invoke_mode = "RESPONSE_STREAM"`
(`infra/lambda.tf`) plus the Lambda Web Adapter
(`AWS_LWA_INVOKE_MODE=response_stream`, `backend/Dockerfile`), which turns
invokes into plain HTTP against uvicorn on `:8080` — the same image runs
unchanged under `docker run` and in Lambda.

### Four settings that each silently break streaming

Mirrored in `infra/README.md` — keep in sync; verify in a browser, not curl.

| Setting | Why it matters |
|---|---|
| `cache_policy_id = Managed-CachingDisabled` on the API behaviors | Any non-zero TTL makes CloudFront buffer the response to store it. |
| `compress = false` on the API behaviors | Compression buffers the body to compress it. |
| `http_version = "http2"` on the distribution | HTTP/3 (QUIC) severs long SSE mid-response; `curl` ignores `alt-svc`, so it passes a curl smoke test and breaks every real visitor. |
| `origin_read_timeout` / `origin_keepalive_timeout` on the Lambda origin | Must be ≥ `var.lambda_timeout_s`, else CloudFront 504s mid-stream. Both are 60s — CloudFront's cap without a quota increase — so the Lambda timeout is capped to match. |

### Auth: `AWS_IAM` Function URL + OAC, never `NONE`

- `authorization_type = "AWS_IAM"` — the org data perimeter 403s `NONE`-auth
  Function URLs. CloudFront signs via a Lambda-typed OAC; one OAC cannot front
  both origin types, so there are two (`infra/cloudfront.tf`).
- **Two grants required, not one** (`infra/lambda.tf`, both scoped to the
  distribution ARN): `lambda:InvokeFunctionUrl` *and* `lambda:InvokeFunction`
  (needed by Function URLs created since October 2025). Missing either 403s
  with the same generic body as a bad signature — the root cause of the 403
  that held the stack up.
- Every POST must carry `x-amz-content-sha256` — the OAC signature covers the
  viewer-supplied payload hash; GET is exempt. Client side:
  [SSE contract](/openwiki/domain/sse-contract.md).
- API behaviors use `Managed-AllViewerExceptHostHeader` — the `Host` header
  must stay the Function URL's own hostname or the signature won't verify.

## Secrets, held in SSM

The repository holds no credential — it can be public — but ADR 0002
deliberately ended the "zero secrets" claim: the model path needs two keys (a
Bedrock key for the LLM calls and an OpenAI key for KB embeddings), plus the
Langfuse keys for tracing. All are data-referenced from SSM, never created by
Terraform.

| Hop | How it authenticates |
|---|---|
| Lambda → Bedrock | Bearer token over HTTPS to the Mantle endpoint (`app/llm.py`); key read at call time from the `AWS_BEARER_TOKEN_BEDROCK` env var, which Terraform pulls from the SSM `SecureString` `/cadre/bedrock-api-key` — created out of band, never committed (ADR 0002). The old `bedrock:InvokeModel` SigV4 grant is deleted. |
| Lambda → OpenAI | One query embedding per in-scope turn (`app/embeddings.py`); `OPENAI_API_KEY` from `/cadre/openai-api-key`, read per request — a rotation needs no cold start |
| Lambda → Langfuse | `LANGFUSE_*` keys from `/cadre/langfuse-*`, read once at container start (`app/tracing.py`) |
| GitHub Actions → AWS | OIDC only, two separate roles, each scoped `ssm:GetParameter` on exactly the parameters its job must resolve |
| CloudFront → Lambda URL | Lambda-typed OAC, SigV4-signed |
| CloudFront → S3 | S3-typed OAC; the bucket blocks all public access |

Every key lands in Terraform state via a decrypted `data` read, so the state
bucket is as sensitive as the keys themselves. `terraform.tfvars`/`backend.hcl`
are gitignored as noise, not secrets. Rotation is an operational task: the
Bedrock and OpenAI keys are resolved per request, so rotating those SSM values
needs no cold start.

## ADR 0001, ADR 0002, ADR 0003, and open tradeoffs

ADR 0001's nine decisions map to `infra/*.tf`,
`.github/workflows/{ci,deploy}.yml`, and `backend/Dockerfile`
(`ENTRYPOINT []` so the base image can't swallow the uvicorn command); ADR 0002
supersedes its Bedrock-auth statements, and ADR 0003 makes `Deploy` the only
workflow that mutates production — it plans and applies Terraform for the same
commit it ships ([CI/CD](/openwiki/workflows/ci-cd.md)), leaving `terraform.yml`
plan-only. Still open:

- A whole turn — the four judge steps, plus condense and one embedding when
  the KB runs, *plus* the brain's generation — must fit inside 60s
  (CloudFront's origin-timeout cap; quota increase to raise). Token budgets in
  `backend/app/config.py` enforce it; `: ping` heartbeats do not extend the
  cap (KB-004).
- The four streaming-breakers still fail no push-gated check. The manual e2e
  dispatch ([CI/CD](/openwiki/workflows/ci-cd.md)) asserts no `Content-Length`
  and real incremental tokens (KB-010, KB-007), but nothing on push boots or
  streams against the real stack.
- `cadre-terraform`'s `ManagedServices` is wildcarded; the real boundary is the
  `production` approval gate.
- Model misconfiguration ships as a *working* chat with amber rails, because
  every guard fails open (KB-009) — the model roster lives only in the image
  (`config.MODEL_DEFAULTS`, issue #84) and the deploy's three gates
  (`assert_models`, `assert_model_env`, `assert_step_models`) are what stop a
  wrong id from ever reaching a visitor.
