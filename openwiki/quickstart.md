---
type: Guide
title: cadre — OpenWiki quickstart
description: Entrypoint to the OpenWiki knowledge base for the cadre streaming chatbot repository. Overview of the stack, the repo layout, the wiki's section pages, and the current backlog.
tags: [quickstart, cadre, entrypoint]
---

# cadre — quickstart

`cadre` is a guardrailed streaming chatbot at `cadre.marcuss.pro`: a React page
(private S3) plus a `POST /ask` endpoint that streams SSE — pipeline step
verdicts, then answer tokens, then `done` — from FastAPI on an arm64 Lambda
container, all behind one CloudFront distribution. The backend is a
[LangGraph conversation engine](/openwiki/domain/sse-contract.md) driving
Bedrock models over the Mantle API (SSE protocol v2) plus OpenAI embeddings for
the knowledge base; the secrets — the Bedrock key ([ADR
0002](/openwiki/architecture/overview.md)), the OpenAI key, the Langfuse keys —
all live in SSM.

Read `adr/README.md` first — ADR 0001 records the load-bearing decisions, ADR
0002 supersedes its Bedrock-auth statements, and ADR 0003 makes `Deploy` the
single gated release path. `infra/README.md` is the living operational doc.
Per-area rules: `backend/CLAUDE.md`, `web/CLAUDE.md`, `infra/CLAUDE.md`.

## What this wiki covers

| Page | What it documents |
|---|---|
| [Architecture overview](/openwiki/architecture/overview.md) | One distribution, two origins; the four silent streaming-breakers; the two-grant 403 trap; the SSM-held secrets (Bedrock + OpenAI + Langfuse, ADR 0002). |
| [SSE contract and steps](/openwiki/domain/sse-contract.md) | Protocol v2: the five events, six pipeline steps, status/outcome semantics, the LangGraph backend, the fetch-SSE client, contract tests. |
| [Terraform infrastructure](/openwiki/infrastructure/terraform.md) | Resource families, variables, the two OIDC roles, Lambda env vars, invariants. |
| [Operations runbooks](/openwiki/operations/runbooks.md) | Bootstrap, two-phase custom domain, 403 bisection, rollback, cost. |
| [CI/CD and deployment](/openwiki/workflows/ci-cd.md) | The six workflows, the approval-gated deploy/rollback pipeline (ADR 0003), MkDocs. |

## Repository layout

```
backend/     FastAPI app + Dockerfile (arm64, Lambda Web Adapter), committed LanceDB KB (`app/kb/`) + ingest pipeline (`ingest/`) — see backend/CLAUDE.md
web/         React + Vite single page, Vitest unit tests + Playwright e2e — see web/CLAUDE.md
infra/       Terraform — CloudFront, Lambda, S3, ACM, OIDC roles — see infra/CLAUDE.md
adr/         Architecture decision records (MADR format), ADR 0001 is the load-bearing one
docs/        MkDocs site (GitHub Pages), nav split by audience — For humans (plan-epic, claude-code, ci-cd, quality/, adr/) and For agents (agents/ = CLAUDE.md wrappers)
.github/     Six workflows (incl. the Diff Honesty Scanner) plus DEPLOYMENT.md (approval-gate setup) and CODEOWNERS
```

The MkDocs site (`docs/` + `mkdocs.yml`) is the public reference; this OpenWiki
is the repo knowledge base. MkDocs embeds `infra/README.md` and `adr/` at build
time rather than copying them.

## Backlog

(Resolved: KB retrieval shipped as plan Phase 3 — `retrieve` condenses, embeds
and searches the committed LanceDB corpus with citations — and Langfuse tracing
shipped as Phase 2, with the `trace` event and public trace links.)
