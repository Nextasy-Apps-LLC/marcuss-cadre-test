---
name: compound-done
description: Close out a compound-workflow issue after Marcus has merged the dev PR — closes the issue, moves it to Done on Project 6, reconciles the learnings PR, and smokes the deployed change. Marcus-only; agents must never invoke this on their own initiative.
---

# compound-done

The final gate of the cycle. **Only Marcus calls this**, after he has reviewed
and merged the dev PR. If this skill was invoked without Marcus explicitly
asking for it in this conversation, stop and say so.

Input: an issue number, e.g. `/compound-done 42`.

## Steps

### 1. Verify the merge — refuse otherwise

Find the dev PR that closes the issue (`search_pull_requests` /
`pull_request_read`). If it is not **merged** (open, draft, or closed
unmerged), STOP and report its actual state. This skill never merges PRs.

### 2. Close and move to Done

1. Close the issue with `state_reason: completed` (GitHub MCP `issue_write`)
   if GitHub's `Closes #<n>` didn't already close it.
2. Move the board item to **Done** per `.claude/compound/kanban.md`. The
   "item closed → Done" automation is the backstop; verify or state which path
   applied. Remove any leftover `status:*` label.

### 3. Reconcile the learnings PR

Find the `KB: learnings from #<n>` PR, if any:

- **Open** → confirm GitHub retargeted it to `main` after the dev branch was
  deleted (fix the base via `update_pull_request` if not), then present its
  entries to Marcus with a recommendation per entry: *new* (merge), *duplicate
  of KB-XXX* (close), or *not KB-worthy* (close). Marcus decides; this skill
  only acts on his answer.
- **Merged or closed** → report that state.
- **Missing** → check the dev PR body said "no new learnings"; if it didn't
  say either, flag the gap (the standing acceptance criterion was missed).

### 4. Post-deploy smoke (when the change shipped to prod)

If the merge triggered/was followed by a deploy:

1. `curl -sS https://cadre.marcuss.pro/healthz` — expect `{"ok": true}`.
2. Offer to run the e2e suite with `BASE_URL=https://cadre.marcuss.pro`.
3. Remember KB-007: curl-green does not prove streaming — remind Marcus to
   watch tokens arrive in a real browser tab for streaming-path changes.

### 5. Epic bookkeeping

If the issue is a sub-issue of an `epic` parent and it was the last open one,
ask Marcus whether to close the parent (same close + Done move if yes).

### 6. Report

One summary: issue state, board column, learnings-PR disposition, smoke
result.
