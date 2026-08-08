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
Bedrock models over the Mantle API (SSE protocol v2); the secrets — the
Bedrock API key ([ADR 0002](/openwiki/architecture/overview.md)) and the
Langfuse keys — live in SSM.

Read `adr/README.md` first — ADR 0001 records the load-bearing decisions, ADR
0002 supersedes its Bedrock-auth statements. `infra/README.md` is the living
operational doc. Per-area rules: `backend/CLAUDE.md`, `web/CLAUDE.md`,
`infra/CLAUDE.md`.

## What this wiki covers

| Page | What it documents |
|---|---|
| [Architecture overview](/openwiki/architecture/overview.md) | One distribution, two origins; the four silent streaming-breakers; the two-grant 403 trap; one secret in SSM (ADR 0002). |
| [SSE contract and steps](/openwiki/domain/sse-contract.md) | Protocol v2: the five events, six pipeline steps, status/outcome semantics, the LangGraph backend, the fetch-SSE client, contract tests. |
| [Terraform infrastructure](/openwiki/infrastructure/terraform.md) | Resource families, variables, the two OIDC roles, Lambda env vars, invariants. |
| [Operations runbooks](/openwiki/operations/runbooks.md) | Bootstrap, two-phase custom domain, 403 bisection, rollback, cost. |
| [CI/CD and deployment](/openwiki/workflows/ci-cd.md) | The five workflows, the approval-gated deploy/rollback pipeline, MkDocs. |

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

- **KB retrieval (`retrieve` step)** — `backend/app/graph/nodes.py` reports
  `skipped`/`kb_not_wired`; plan.md Phase 3 (query condensing, LanceDB search,
  citations) is not built.

(Two earlier backlog items are resolved: Langfuse tracing shipped as plan
Phase 2 — the `trace` event, public trace links, and the client "View trace"
chip all exist — and `docs/ci-cd.md` now counts all five workflows.)
