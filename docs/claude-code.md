# The Claude Code workflow

This page is the map of how the repo is *built with* Claude Code — the
compound workflow, the skills and subagents, the knowledge base, and the
guardrails that keep AI-written changes honest. It links to the evidence
rather than restating it; the artifact each link names is the source of
truth.

## The compound loop

Every feature, fix and chore flows through GitHub issues on
[Project 6](https://github.com/orgs/Nextasy-Apps-LLC/projects/6)
(**Backlog → In Progress → In Review → Done**), driven by four skills with
strict roles. Never implement without an issue; never skip a column.

| Stage | Skill | What it does |
|---|---|---|
| **Issue** | [`/compound-create-issue`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.claude/skills/compound-create-issue/SKILL.md) | Writes a decision-free technical spec from a feature description, injecting the applicable KB entries via the `kb-filter` subagent so the main session never loads the whole KB. Adds the issue to the board in Backlog. |
| **Implement** | [`/compound-implement`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.claude/skills/compound-implement/SKILL.md) | TDD: failing unit tests committed first, then code, then e2e against real endpoints. Opens the dev PR plus a stacked learnings PR. |
| **Review** | [`/compound-review`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.claude/skills/compound-review/SKILL.md) | Checks quality and spec adherence; posts exactly one verdict — APPROVE, APPROVE WITH MINOR COMMENTS, or REJECT — BLOCKERS. Never pushes fixes. |
| **Done** | [`/compound-done`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.claude/skills/compound-done/SKILL.md) | Marcus-only, after merge: closes the issue, reconciles the learnings PR, smokes production. |

Three [subagents](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/tree/main/.claude/agents)
carry the roles: `kb-filter` (haiku, KB lookup without the context cost),
`compound-implementor`, `compound-reviewer`. Board mechanics — including the
GraphQL recipe for moving cards — live in
[`.claude/compound/kanban.md`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.claude/compound/kanban.md);
the issue shape is the
[compound feature template](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.github/ISSUE_TEMPLATE/compound-feature.md).

## The Knowledge Base loop

[`kb/learnings.json`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/kb/learnings.json)
is what makes the workflow *compound*: 24 entries at this writing
(KB-001…KB-024), each a gotcha that cost real time once and never again.
Issue creation reads it; implementation appends to it through a stacked PR
that touches only the KB file, so Marcus accepts or rejects captured
knowledge independently of the code. Entries are cited in the commits that
honor them — `git log --grep=KB-` shows the loop working.

## The Diff Honesty Scanner

[#86](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/issues/86) added
a CI gate that fails any PR whose diff *weakens the safety net* — deleting or
softening tests, skipping gates, widening fail-open paths. The
[scanner](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/.github/scripts/diff_honesty_scanner.py)
is stdlib-only, twelve rule families, and its
[own 94-case fixture suite](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/tree/main/.github/tests)
runs first in the same job: a scanner that cannot catch its fixture
violations fails red before it judges anyone. Waivers are explicit
`honesty-waiver:` lines in the PR body — reviewable text, not silence — and a
diff that modifies the scanner machinery is never waivable.

## The quality loop

Answer quality is a measured loop, not a vibes loop: every fix follows the
same five steps, judged by models chosen on benchmark numbers rather than
brand. [How answers get better](quality/cadre-ai-agent.md) owns the process
and the record; [`backend/evals/`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/backend/evals/README.md)
is the judge-benchmark harness that picked the current roster;
[what a turn costs](quality/costs.md) keeps the model choices honest about
price.

## Where the TDD evidence lives

The failing-suite-first commit pairs (tests red, then implementation green)
are listed with their SHAs in the
[review walkthrough](review.md#evaluation-dimensions-evidence) — one list,
kept there, linked here. The agent-facing half of this strategy — the
`CLAUDE.md` family and the generated `openwiki/` knowledge base — is mapped
on [For agents](agents/index.md).
