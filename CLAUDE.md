# CLAUDE.md — marcuss-cadre-test

`cadre` — a guardrailed streaming chatbot at `cadre.marcuss.pro`: a React page
plus a `POST /ask` SSE endpoint on one CloudFront distribution (private S3 +
`AWS_IAM` Lambda Function URL), backed by Bedrock.

**Read `adr/README.md` first.** ADR 0001 records the load-bearing decisions.
Don't fight it without a superseding ADR. Keep `infra/README.md` in sync.

**Scoped rules:** `infra/CLAUDE.md` (Terraform), `web/CLAUDE.md` (React/SSE
client), `backend/CLAUDE.md` (FastAPI/SSE server) — each loads automatically
when you touch files under its directory. Read them before editing there.

## How work happens here — the compound workflow

All feature/fix/chore work flows through GitHub issues on
[Project 6](https://github.com/orgs/Nextasy-Apps-LLC/projects/6)
(**Backlog → In Progress → In Review → Done**), driven by four skills with
strict roles. **Never implement without an issue**, and never skip a column.

| Skill | Role | Board move |
|---|---|---|
| `/compound-create-issue` | writes a decision-free spec, injecting applicable `kb/learnings.json` entries via the `kb-filter` agent; epics = `epic` label + native sub-issues | → Backlog |
| `/compound-implement <n>` | TDD (failing unit tests → code → e2e vs real endpoints), dev PR + stacked learnings PR | Backlog → In Progress → In Review |
| `/compound-review <pr>` | quality + spec adherence; one verdict: APPROVE / APPROVE WITH MINOR COMMENTS / REJECT — BLOCKERS | none (stays In Review) |
| `/compound-done <n>` | **Marcus only**, after he merges: close issue, reconcile learnings PR, smoke prod | → Done |

The Knowledge Base `kb/learnings.json` is the compounding loop: issue creation
reads it, implementation appends to it via a separate PR (stacked on the dev
branch, only the KB file in the diff) that Marcus accepts or rejects
independently of the code. Board-move mechanics: `.claude/compound/kanban.md`.

<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
