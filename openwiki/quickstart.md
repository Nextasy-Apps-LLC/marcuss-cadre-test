---
type: Guide
title: cadre — OpenWiki quickstart
description: Entrypoint to the OpenWiki knowledge base for the cadre streaming chatbot repository. Overview of the stack, the repo layout, the wiki's section pages, and the current backlog.
tags: [quickstart, cadre, entrypoint]
---

# cadre — quickstart

`cadre` is a guardrailed streaming chatbot, live at `cadre.marcuss.pro`. A React
page (served from a private S3 bucket) and a `POST /ask` endpoint that streams
Server-Sent Events — rail verdicts first, then answer tokens, then a terminal
`done` event — from a FastAPI backend running as an arm64 container on Lambda,
fronted by one CloudFront distribution. The backend is deliberately a
[walking skeleton](/openwiki/domain/sse-contract.md): the SSE plumbing is real
end-to-end, but the "brain" is a stub so the streaming path can be proven before
any model is wired in.

Start with `adr/README.md` in the repo and its single
[ADR 0001](/openwiki/architecture/overview.md) — they record the load-bearing
decisions and the traps that cost real time. `infra/README.md` is the living
operational doc. Scoped guidelines in `backend/CLAUDE.md`, `web/CLAUDE.md`, and
`infra/CLAUDE.md` carry per-area rules for agents and humans.

## The stack in one paragraph

One CloudFront distribution, one hostname, two origins: a **private S3 bucket**
is the default behavior (cached, compressed) for the page; a **Lambda Function
URL** in `RESPONSE_STREAM` invoke mode serves `/ask`, `/healthz`, `/config` as
ordered cache behaviors with caching and compression deliberately off. The
Function URL is `AWS_IAM` — CloudFront signs every request via a Lambda-typed
OAC — and the Lambda talks to Bedrock with execution-role SigV4. There is no API
Gateway anywhere (it buffers), and no static credential anywhere (OIDC-only CI,
OAC-only origin auth). See the [architecture overview](/openwiki/architecture/overview.md).

## What this wiki covers

| Page | What it documents |
|---|---|
| [Architecture overview](/openwiki/architecture/overview.md) | The streaming stack: one distribution, two origins, RESPONSE_STREAM + Lambda Web Adapter, zero secrets, the four silent streaming-breakers, and ADR 0001's decisions. |
| [SSE contract and rails](/openwiki/domain/sse-contract.md) | The four-event wire format, the six rails, rail-status semantics (degraded / blocked / lost / skipped), the walking-skeleton backend, the hand-rolled fetch-SSE client, and the tests that pin the contract. |
| [Terraform infrastructure](/openwiki/infrastructure/terraform.md) | The Terraform module: resource families, variables and outputs, the two OIDC roles (cadre-deploy vs cadre-terraform), Lambda env vars, and the streaming-breaker checklist. |
| [Operations runbooks](/openwiki/operations/runbooks.md) | First apply, the two-phase custom domain attach via Cloudflare, 403 bisection, rollback, and cost. |
| [CI/CD and deployment](/openwiki/workflows/ci-cd.md) | The five GitHub Actions workflows, the manual deploy/rollback pipeline, the approval gate, and the MkDocs documentation site. |

## Repository layout

```
backend/     FastAPI app + Dockerfile (arm64, Lambda Web Adapter) — see backend/CLAUDE.md
web/         React + Vite single page, Vitest unit tests — see web/CLAUDE.md
infra/       Terraform — CloudFront, Lambda, S3, ACM, OIDC roles — see infra/CLAUDE.md
adr/         Architecture decision records (MADR format), ADR 0001 is the load-bearing one
docs/        The MkDocs site published to GitHub Pages (wrappers include adr/ and infra/)
.github/     Five workflows plus DEPLOYMENT.md (approval-gate setup) and CODEOWNERS
```

The user-facing documentation site (MkDocs, built from `docs/` + `mkdocs.yml`)
and this OpenWiki knowledge base coexist: the MkDocs pages are the public
reference docs; the OpenWiki pages are the repo knowledge base for agents and
maintainers. Both summarize the same [Terraform infrastructure](/openwiki/infrastructure/terraform.md)
and [SSE contract](/openwiki/domain/sse-contract.md); the MkDocs `infrastructure.md`
and `docs/adr/` pages embed `infra/README.md` and `adr/` at build time rather
than copying them.

## Key facts to remember

- **No API Gateway.** HTTP APIs and any non-zero-TTL cache policy or compression
  buffer the response — streaming silently becomes one blob at the end.
- **Four settings each defeat streaming on their own**: cache policy must be
  `CachingDisabled`, `compress = false`, `http_version = "http2"` (HTTP/3 severs
  SSE), and origin timeouts must be ≥ the Lambda timeout (both capped at 60s).
- **Two Lambda grants, not one**: `lambda:InvokeFunctionUrl` *and*
  `lambda:InvokeFunction` — missing either 403s identically to a bad signature.
- **Every POST carries `x-amz-content-sha256`**; without it the OAC signature
  fails and Lambda answers 403 "signature does not match".
- **Shipping is manual**: `deploy.yml` is dispatch-only, gated behind a
  `production` environment approval, and rolls back by re-tagging an image that
  already passed CI.

## Backlog

- **Real Bedrock brain** (`backend/app/main.py`, the `_reply_for()` seam) — the
  six rails are emitted but the model calls for rails 2–6 and the brain (rail 4)
  are stubs; env vars `CADRE_BRAIN_MODEL`, `CADRE_JUDGE_MODEL`, `CADRE_GUARD_MODEL`,
  `CADRE_BRAIN_EFFORT` are already injected by `infra/lambda.tf` but not yet
  consumed by backend code. Deferred: not implemented in source yet.
- **`docs/ci-cd.md` workflow count** — the MkDocs page still says "four
  workflows" and does not mention `openwiki-update.yml`; a source-docs fix, not
  an OpenWiki fix. Deferred: tracked here so it isn't silently lost.
