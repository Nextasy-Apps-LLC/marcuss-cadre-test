---
name: Compound feature
about: A feature/fix/chore flowing through the compound engineering workflow (Backlog → In Progress → In Review → Done)
labels: compound
---

<!--
Issues in this repo are written to be implementable WITHOUT big decisions:
architecture, contracts, and scope are resolved here, not by the implementor.
Normally created via the /compound-create-issue skill, which also fills the
"Applicable learnings" section from kb/learnings.json.
-->

## Context

<!-- Why this change: the problem or need, what prompted it, intended outcome. -->

## Technical spec

<!-- Files to touch, functions/contracts, exact expected behavior. Detailed
enough that the implementor makes no big decisions. -->

## Acceptance criteria

<!-- Feature-specific criteria first, then the three standing criteria below —
they apply to EVERY issue and stay in. -->

- [ ] …
- [ ] TDD evidence: unit tests written first and failing before implementation
- [ ] e2e suite green against real endpoints (`BASE_URL`-pointable; local = real backend image in docker with real AWS credentials)
- [ ] Learnings PR opened, stacked on the dev branch, touching only `kb/learnings.json` — OR the dev PR body states "no new learnings" explicitly

## Applicable learnings

<!-- KB entries selected for this feature (id, title, detail). Treat each as a
requirement. "None found in KB." if the filter returned nothing. -->

## Out of scope

<!-- What an eager implementor might wrongly include. -->

---

<details>
<summary>Learnings taxonomy — what must be persisted to the KB</summary>

While implementing, capture anything matching `kb/learnings.json` `_taxonomy`:

- **gotcha** — a trap that silently breaks something when you don't know about
  it. Written as: what breaks, and what to do instead.
- **debug** — a symptom→cause mapping that cost real time. Written as: symptom,
  actual cause, fastest diagnostic path.
- **learning** — a reusable practice or heuristic to apply next cycle.

An entry qualifies only if the NEXT cycle would act differently because of it.
New entries go in a separate PR stacked on the dev branch (only
`kb/learnings.json` in the diff) so the code and the learnings can be accepted
or rejected independently.
</details>
