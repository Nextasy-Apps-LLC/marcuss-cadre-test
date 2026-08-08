---
type: Guide
title: cadre — OpenWiki quickstart
description: Entrypoint to the OpenWiki knowledge base for the cadre streaming chatbot repository. Overview of the stack, the repo layout, the wiki's section pages, and the current backlog.
tags: [quickstart, cadre, entrypoint]
---

# cadre — quickstart

`cadre` is a guardrailed streaming chatbot at `cadre.marcuss.pro`: a React page
(private S3) plus a `POST /ask` endpoint that streams SSE — a trace frame, then
pipeline step verdicts with per-step timing, then answer tokens, then `done` —
from FastAPI on an arm64 Lambda container, all behind one CloudFront
distribution. The backend is a [LangGraph conversation
engine](/openwiki/domain/sse-contract.md) driving Bedrock models over the
Mantle API (SSE protocol v2), grounding answers in a committed
[knowledge base](/openwiki/domain/knowledge-base.md) (LanceDB + OpenAI
embeddings), with every turn traced to
[Langfuse](/openwiki/architecture/overview.md). The Bedrock, OpenAI and
Langfuse secrets live out-of-band in SSM per
[ADR 0002](/openwiki/architecture/overview.md).

Read `adr/README.md` first — ADR 0001 records the load-bearing decisions, ADR
0002 supersedes its Bedrock-auth statements. `infra/README.md` is the living
operational doc. Per-area rules: `backend/CLAUDE.md`, `web/CLAUDE.md`,
`infra/CLAUDE.md`.

## What this wiki covers

| Page | What it documents |
|---|---|
| [Architecture overview](/openwiki/architecture/overview.md) | One distribution, two origins; the four silent streaming-breakers; the two-grant 403 trap; the five SSM secrets (Bedrock, OpenAI, Langfuse); the in-image KB and tracing hops. |
| [SSE contract and steps](/openwiki/domain/sse-contract.md) | Protocol v2: the five events (`trace`, `state` with `elapsed_ms`, `token`, `done`, `error`), six pipeline steps, status/outcome semantics, the LangGraph backend, the fetch-SSE client, contract tests. |
| [Knowledge base and retrieval](/openwiki/domain/knowledge-base.md) | The committed LanceDB corpus, the condense→embed→search retrieve step, citations, the offline ingest pipeline, and the fail-open footguns (manifest mismatch, container-init warm-up). |
| [Terraform infrastructure](/openwiki/infrastructure/terraform.md) | Resource families (incl. the OpenAI + Langfuse SSM modules), variables, the two OIDC roles, the Lambda env vars, invariants. |
| [Operations runbooks](/openwiki/operations/runbooks.md) | Bootstrap, two-phase custom domain, 403 bisection, rollback, KB refresh, cost. |
| [CI/CD and deployment](/openwiki/workflows/ci-cd.md) | The five workflows, the approval-gated deploy/rollback pipeline, the three e2e gates, MkDocs. |

## Repository layout

```
backend/     FastAPI app + Dockerfile (arm64, Lambda Web Adapter) — see backend/CLAUDE.md
web/         React + Vite single page, Vitest unit tests — see web/CLAUDE.md
infra/       Terraform — CloudFront, Lambda, S3, ACM, OIDC roles — see infra/CLAUDE.md
adr/         Architecture decision records (MADR format), ADR 0001 is the load-bearing one
docs/        The MkDocs site published to GitHub Pages (wrappers include adr/ and infra/)
.github/     Five workflows plus DEPLOYMENT.md (approval-gate setup) and CODEOWNERS
```

The MkDocs site (`docs/` + `mkdocs.yml`) is the public reference; this OpenWiki
is the repo knowledge base. MkDocs embeds `infra/README.md` and `adr/` at build
time rather than copying them.

## Backlog

- **`docs/index.md` protocol page** — the MkDocs site still describes "four
  event types plus a `: ping`" and "exactly one secret", both stale since the
  `trace` event and the five SSM parameters; a source-docs fix.
