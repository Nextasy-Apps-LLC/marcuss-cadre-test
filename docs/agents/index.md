# For agents

This section is the machine-facing half of the documentation: the files an AI
coding agent reads to keep building the system correctly. Humans are better
served by the [review walkthrough](../review.md) — but if you are reviewing
the *Claude Code proficiency* dimension of this submission, this page is the
map of the agent-documentation strategy.

## The `CLAUDE.md` family — loaded automatically, scoped by directory

Claude Code reads `CLAUDE.md` at session start, plus the scoped file for any
directory being edited. Each is short, opinionated, and points at the next
layer down rather than restating it:

| File | Loads when | Owns |
|---|---|---|
| [Root `CLAUDE.md`](claude-root.md) | every session | strategy, phase status, the compound workflow contract |
| [`backend/CLAUDE.md`](backend.md) | edits under `backend/` | the SSE contract, graph rules, prompts-in-files |
| [`web/CLAUDE.md`](web.md) | edits under `web/` | the SSE client, reducer conventions |
| [`infra/CLAUDE.md`](infra.md) | edits under `infra/` | Terraform rules and their off-limits knobs |

## `openwiki/` — the generated knowledge base

[`openwiki/`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/tree/main/openwiki)
is a recurring code wiki: architecture, domain concepts (the SSE contract),
workflows, operations runbooks, and source maps, regenerated from the actual
tree by the daily `openwiki-update.yml` workflow and landed as a pull request —
generated docs go through the same review history as everything else.
`openwiki/INSTRUCTIONS.md` caps the whole wiki at roughly 1,200–1,500 words so
an agent can load it inside a five-minute prose budget.

!!! warning "Machine-generated, on a schedule"
    The wiki is regenerated daily from the tree, so between runs it can trail
    `main`. Treat its pages as a map, not as gospel — the code and the ADRs
    win any disagreement, and `.last-update.json` records the exact commit it
    was generated from.

**These pages are deliberately not republished on this site.** Four reasons:

1. **Freshness.** The wiki lags main by up to a day (and has lagged by over a
   hundred commits after a heavy merge week). Publishing a snapshot would put
   claims the generator has already corrected on the human-facing site.
2. **Link shape.** The generated pages cross-link with root-absolute
   `/openwiki/...` paths that resolve on GitHub, not under GitHub Pages —
   every link would 404 here.
3. **Robustness.** Wrapping each generated file would break the `--strict`
   build the day the generator adds or drops a page. Nothing in `docs/` or
   `mkdocs.yml` is inside the generator's `add-paths` (`openwiki`,
   `AGENTS.md`, `CLAUDE.md`, the workflow itself), so this structure survives
   every future regeneration untouched.
4. **Audience fit.** The wiki is word-budgeted for agent context windows;
   mirroring it here would dilute a site meant for a time-limited human. The
   split *is* the point: agents read `openwiki/`, humans read this site.

## `kb/learnings.json` — the compounding loop

[`kb/learnings.json`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/kb/learnings.json)
is the Knowledge Base the compound workflow compounds into: every cycle reads
it at issue-creation time (via the `kb-filter` agent) and appends to it
through a stacked learnings PR Marcus accepts or rejects independently of the
code. Each entry is cited in the commits that honor it. The full loop is on
[the Claude Code workflow](../claude-code.md) page.
