# Architecture Decision Records

This folder captures the load-bearing decisions about how `cadre` — the
guardrailed streaming chatbot at `cadre.marcuss.pro` — is built and deployed.
Each record is a single Markdown file in [MADR](https://adr.github.io/madr/)
format: title, status, context, decision, consequences. Same house style as
`marcuss.pro/adr/`.

ADRs are append-only. If a decision is superseded, write a new ADR that
supersedes the old one and update the old one's status — don't edit history.

## How to write a new ADR

1. Pick the next free number (last is `0001`).
2. File name: `NNNN-kebab-case-title.md`.
3. Status starts as `Proposed`; promote to `Accepted` when it's actually
   being followed.
4. Keep it short. ADRs are decisions, not designs — link out for designs.
   A single ADR covering several tightly-coupled decisions (as 0001 does) is
   fine when splitting them would just scatter cross-references; split into
   separate files once the decisions start evolving independently.

## Current ADRs

| # | Title | Status |
|---|---|---|
| [0001](0001-streaming-chatbot-cloudfront-lambda-s3.md) | Streaming chatbot on one CloudFront distribution, IAM-only Lambda, zero secrets | Accepted |
