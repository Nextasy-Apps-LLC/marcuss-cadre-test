---
name: compound-create-issue
description: Create a compound-workflow GitHub issue (or epic with sub-issues) from a feature description or plan section. Consults the Knowledge Base via the kb-filter agent, writes a decision-free technical spec, adds the issue to Project 6 in Backlog. Use when starting any new feature, fix, or chore — no work happens in this repo without an issue created this way.
---

# compound-create-issue

Turns a feature description or plan section into implementable GitHub issue(s)
on the Project 6 board. The issue must be detailed enough that the implementor
makes **no big decisions** — every open architecture, contract, or scope
question gets resolved here (ask Marcus if needed), not downstream.

## Steps

### 1. Distill the matching summary

From the input (full plan section, paragraphs, or a rough idea — keep all of it
for step 3), write a 3–5 line summary stating:

- **what** is being built,
- **areas** touched, using the KB vocabulary: `backend` / `web` / `infra` /
  `ci` / `process`,
- **surfaces/technologies** involved (SSE, Lambda, CloudFront, Bedrock,
  Terraform, GitHub Actions, …).

This summary exists only as the KB matching key.

### 2. Filter the Knowledge Base

Spawn the `kb-filter` agent (Agent tool, `subagent_type: "kb-filter"`,
`run_in_background: false`) with the summary as its prompt. It reads
`kb/learnings.json` itself — do NOT read the full KB into this session, and do
NOT pass the full plan to the agent. It returns only the applicable entries, or
`NO_APPLICABLE_ENTRIES`.

### 3. Draft the issue

Use the structure of `.github/ISSUE_TEMPLATE/compound-feature.md`:

- **Context** — why this change, what prompted it, intended outcome.
- **Technical spec** — files to touch, functions/contracts, exact behavior.
  Resolve every big decision here. If the input leaves one open, ask Marcus
  (AskUserQuestion) before creating the issue.
- **Acceptance criteria** — checklist. ALWAYS includes these standing items,
  verbatim, in addition to the feature-specific ones:
  - [ ] TDD evidence: unit tests written first and failing before implementation
  - [ ] e2e suite green against real endpoints (`BASE_URL`-pointable; local = real backend image in docker with real AWS credentials)
  - [ ] Learnings PR opened, stacked on the dev branch, touching only `kb/learnings.json` — OR the dev PR body states "no new learnings" explicitly
- **Applicable learnings** — the kb-filter output inlined, with KB ids. If
  `NO_APPLICABLE_ENTRIES`, write "None found in KB."
- **Out of scope** — what an eager implementor might wrongly include.

### 4. Epic or single issue?

If the work decomposes into independently implementable, reviewable pieces
(each its own PR), make it an **epic**:

- Parent issue: label `epic`, body = Context + the decomposition + links.
- One child issue per piece via steps 1–3 (each child gets its own kb-filter
  pass scoped to that piece).
- Attach children with the GitHub MCP `sub_issue_write` tool (native
  sub-issues). Org issue types are not accessible to this app — the `epic`
  label plus sub-issues is the mechanism.

Single-PR-sized work is one plain issue; do not manufacture epics.

### 5. Create and place on the board

1. Create via GitHub MCP `issue_write` (search for duplicates first with
   `search_issues`). Apply labels: `compound`, plus `epic` if applicable.
2. Add to Project 6 and set Status = **Backlog** following
   `.claude/compound/kanban.md` (gh GraphQL path, or the `status:backlog`
   label fallback — report which one ran).
3. Reply with the issue URL(s) and the KB entries that were inlined.
