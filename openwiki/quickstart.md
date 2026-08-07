---
type: Guide
title: cadre — OpenWiki quickstart
description: Entrypoint to the OpenWiki knowledge base for the cadre streaming chatbot repository. Overview of the stack, the repo layout, the wiki's section pages, and the current backlog.
tags: [quickstart, cadre, entrypoint]
---

# cadre — quickstart

`cadre` is a guardrailed streaming chatbot at `cadre.marcuss.pro`: a React page
(private S3) plus a `POST /ask` endpoint that streams SSE — rail verdicts, then
answer tokens, then `done` — from FastAPI on an arm64 Lambda container, all
behind one CloudFront distribution. The backend is a
[walking skeleton](/openwiki/domain/sse-contract.md): the SSE plumbing is real
end-to-end, the "brain" is a stub.

Read `adr/README.md` first — ADR 0001 records the load-bearing decisions.
`infra/README.md` is the living operational doc. Per-area rules:
`backend/CLAUDE.md`, `web/CLAUDE.md`, `infra/CLAUDE.md`.

## What this wiki covers

| Page | What it documents |
|---|---|
| [Architecture overview](/openwiki/architecture/overview.md) | One distribution, two origins; the four silent streaming-breakers; the two-grant 403 trap; zero secrets. |
| [SSE contract and rails](/openwiki/domain/sse-contract.md) | The four-event wire format, six rails, client rail-status semantics, the fetch-SSE client, contract tests. |
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

- **Real Bedrock brain** — `_reply_for()` in `backend/app/main.py` is the seam;
  the `CADRE_*_MODEL` / `CADRE_BRAIN_EFFORT` env vars are already injected by
  `infra/lambda.tf` but not yet read by backend code.
- **`docs/ci-cd.md` workflow count** — still says "four workflows", missing
  `openwiki-update.yml`; a source-docs fix.
