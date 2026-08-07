# CLAUDE.md — marcuss-cadre-test

`cadre` — a guardrailed streaming chatbot at `cadre.marcuss.pro`: a React page
plus a `POST /ask` SSE endpoint on one CloudFront distribution (private S3 +
`AWS_IAM` Lambda Function URL), backed by Bedrock.

**Read `adr/README.md` first.** ADR 0001 records the load-bearing decisions.
Don't fight it without a superseding ADR. Keep `infra/README.md` in sync.

**Scoped rules:** `infra/CLAUDE.md` (Terraform), `web/CLAUDE.md` (React/SSE
client), `backend/CLAUDE.md` (FastAPI/SSE server) — each loads automatically
when you touch files under its directory. Read them before editing there.
