---
name: compound-implement
description: Implement a compound-workflow issue TDD-style and open the dev PR plus (if warranted) the stacked learnings PR. Moves the issue Backlog → In Progress → In Review on the Project 6 board. Use with an issue number, e.g. "/compound-implement 42".
---

# compound-implement

Implements exactly one issue created by `compound-create-issue`. The issue is
the spec; this skill adds no scope and takes no big decisions.

## Steps

### 1. Load and gate

1. Read the issue (GitHub MCP `issue_read`) including its **Applicable
   learnings** section — those KB entries are constraints, not suggestions.
2. Verify the issue is in **Backlog** (board column, or `status:backlog`
   label). If it is already In Progress or beyond, stop and report — someone
   else may own it.
3. If the spec leaves a genuinely big decision open, STOP and report the gap
   (that's a bug in the issue — route it back through `compound-create-issue`).

### 2. Move to In Progress

Follow `.claude/compound/kanban.md`. Report which path ran.

### 3. Branch

Worktree off fresh `origin/main`, branch `feat/issue-<n>-<slug>` (use `fix/` /
`chore/` / `docs/` prefixes when the issue is of that type). Never work on an
existing branch or the shared clone's checkout.

### 4. TDD loop (in order, no skipping)

1. **Unit tests first.** Write the tests the acceptance criteria imply; run
   them; confirm they FAIL for the right reason. Commit them (this commit is
   the TDD evidence the reviewer looks for).
2. **Implement** until unit tests pass.
3. **E2E against real endpoints.** Extend/point the e2e suite via `BASE_URL`;
   run it against the real backend image in local docker with real AWS
   credentials (real Bedrock — no mock server, no faked responses). Without
   AWS credentials in this session, run everything that works without them and
   say plainly which e2e portion is deferred and why.
4. Iterate until green. Paste real command output in the PR body — never claim
   green unverified.

Honor every KB entry from the issue; cite the KB id in the commit that honors
it.

### 5. Dev PR → In Review

1. Push (`git push -u origin <branch>`, retry on network errors: 2s/4s/8s/16s).
2. Open the PR with `Closes #<n>` in the body, plus: what was built, test
   output, per-acceptance-criterion checklist, and either the learnings-PR
   link (step 6) or the explicit line **"No new learnings."**
3. Move the issue to **In Review** per `.claude/compound/kanban.md`.
4. Comment a short hand-off summary on the issue (what changed, PR link,
   anything the reviewer should look at first).

### 6. Learnings PR (only if something new was learned)

Compare what this cycle taught against the KB taxonomy in
`kb/learnings.json` (`_taxonomy`). An entry qualifies only if the NEXT cycle
would act differently because of it; repeats of existing entries do not.

If there are new entries:

1. Branch `learnings/issue-<n>` **off the dev branch** (not main).
2. Edit only `kb/learnings.json`: append entries following `_entry_contract`
   (next sequential id, `source` = this issue/PR, today's date).
3. Push and open a PR **based on the dev branch** titled
   `KB: learnings from #<n>`. The diff must show only the KB change. Do not
   add `Closes #<n>` — the dev PR owns closing the issue.
4. When the dev branch merges and is deleted, GitHub retargets this PR to
   main; Marcus then merges (accept) or closes (reject/duplicate) it
   independently.
