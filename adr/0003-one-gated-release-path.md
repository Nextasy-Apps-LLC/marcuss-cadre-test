# ADR 0003 — One gated release path: `Deploy` plans and applies Terraform

- **Status:** Accepted
- **Supersedes:** [ADR 0001](0001-streaming-chatbot-cloudfront-lambda-s3.md) decision 7 (Terraform in CI) and the apply-ownership half of decision 8
- **Date:** 2026-08-08

## Context

`cadre` had two ways to change production. `.github/workflows/deploy.yml`
shipped the image, the page and the CloudFront invalidation for a given commit.
`.github/workflows/terraform.yml` applied infrastructure on its own dispatch.
Both were `workflow_dispatch`, both sat behind the `production` environment
gate, and **nothing forced them to run against the same commit — or at all.**

They stopped agreeing, and it took weeks to notice. Issue #84 moved the model
roster out of Terraform: `infra/lambda.tf` stopped setting `CADRE_MODEL_*`
because a Terraform variable was silently beating the code default, so
production ran models that no deployed commit had been benchmarked against.
That fix shipped. No `terraform apply` followed it. The function therefore kept
the seven `CADRE_MODEL_*` variables Terraform had set before the change, and
kept running the old roster — invisibly, because every model step fails open
(KB-009), so the wrong models render as a perfectly healthy chat.

The `assert_model_env` gate added in #84 eventually caught it and refused the
deploy. That was correct and it was also a dead end: **the deploy had no way to
fix the environment it had just refused to run against**, because applying
Terraform was a different workflow that the deploy could not invoke. A gate with
no remedy behind it is a stop sign in front of a wall.

Two things that look like the obvious fix are not, and are recorded here
because both were proposed:

1. **"Let a single `terraform apply -var image_tag=$SHA` be the only thing that
   mutates production, so the image URI and the model env are written
   atomically."** This requires deleting `lifecycle { ignore_changes =
   [image_uri] }` from `aws_lambda_function.this`. Measured against live state,
   a plan with that line removed emits
   `~ image_uri = ".../cadre:<running sha>" -> ".../cadre:bootstrap"` — it rolls
   production back to the bootstrap image. The guard is not vestigial; it is the
   only reason `var.image_tag`'s `"bootstrap"` default is harmless.
2. **"Have Terraform write the model environment atomically with the image."**
   That is precisely the defect #84 removed. Model ids belong in the artifact
   that carries the prompts they were measured against.

So the atomicity that was actually needed is not "one API call writes both
values". It is **one commit, one approval, one run — applying that commit's
infrastructure and that commit's image together.**

## Decision

### 1. `Deploy` is the only workflow that mutates production

`.github/workflows/deploy.yml`, `name: Deploy`, `workflow_dispatch` only,
inputs unchanged (`action: deploy | rollback`, a 40-character `sha`). It now
runs Terraform as part of the release.

`terraform.yml` becomes **plan-only**: its `apply` job and the `apply` choice
are deleted, and it declares no `production` environment, so it is structurally
incapable of changing anything or of sitting behind the gate pretending to be a
release path. It is for reading — an infra diff on a pull request, or an
on-demand drift check. Two workflows that both looked like the way to ship is
the ambiguity that caused this ADR; one of them is now obviously not.

An infrastructure-only change is a `Deploy` at the currently-running SHA: the
image already exists in ECR, so the build is skipped and only the apply works.

### 2. Plan before the gate; apply exactly that plan after it

Job `plan` (ungated) validates the SHA, checks out **that SHA's tree**, verifies
the target image in ECR, then runs `terraform plan -out=tfplan` against that
commit's `infra/` and uploads the plan as an artifact. Job `release`
(`environment: production`) downloads it and runs `terraform apply tfplan`.

Applying a saved plan rather than re-planning is inherited from ADR 0001
decision 7 and is kept deliberately: if state moved between approval and apply,
Terraform refuses rather than applying something nobody reviewed.

### 3. The approval gate is unconditional

Every release — deploy and rollback alike — pauses on the `production`
environment for a human. The `release` job carries no `if:`, and no input can
skip it.

**An auto-proceed path was designed, considered, and rejected by Marcus**:
auto-approve when the plan's changes are confined to the Lambda function,
require a human otherwise. It is recorded here so it is not re-introduced as an
optimisation. The reasoning: a mechanism that can classify one plan as routine
will eventually classify a destructive one as routine, and the failure mode is
silent and unattended. The blast radius of the alternative is one click per
release.

What survives is the useful half. `.github/scripts/summarize_plan.py` renders
the plan into the run summary before the gate — every resource and action, with
anything beyond `aws_lambda_function.this` flagged for a closer read — so the
approver decides informed rather than ceremonially. It is **advisory**: it
returns 0 for every plan it can parse, however destructive, and its tests pin
that property.

### 4. The apply runs *before* the model gates

Inside the gated job the order is: `terraform apply` → `assert_models` →
`assert_model_env` → build → `update-function-code` → frontend → smoke →
`assert_step_models`.

The apply comes first because it is the remedy. Terraform declares the whole
`environment.variables` map, so any `CADRE_MODEL_*` it does not manage — a
leftover, or a hand-set break-glass override — is deleted by the apply. Gating
first would deadlock the release against drift only the apply can clear.

`assert_model_env` is thereby made **redundant, on purpose**. It stays: it is
nearly free, it has already caught one real production failure, and it is the
only thing that would catch a variable set by hand between the plan and the
build.

### 5. Two roles, assumed in sequence

`cadre-deploy` (resource-scoped, ships code) and `cadre-terraform`
(service-scoped, runs Terraform) are **not merged** — ADR 0001 decision 5
stands, and `cadre-deploy` must never gain `lambda:UpdateFunctionConfiguration`.
Each job calls `configure-aws-credentials` more than once, re-assuming the
narrower role for each phase, so no step holds privileges its phase does not
need. A `workflow_call` interface was rejected as more machinery for the same
boundary.

No IAM change was required: `local.deploy_subs` and `terraform_assume` already
list `ref:refs/heads/main`, `pull_request` and `environment:production` across
both repo spellings. That was verified before the workflow was written, because
KB-006 is that an environment-gated job's OIDC `sub` **replaces** the ref form
and the resulting denial only reproduces after the approval click.

### 6. `image_uri` keeps exactly one owner: the deploy step

`lifecycle { ignore_changes = [image_uri] }` stays on
`aws_lambda_function.this`, and `Deploy` never passes `-var image_tag`. The
`update-function-code` step is the single writer.
`.github/tests/test_release_workflow.py` pins the line so it cannot be removed
by a tidy-looking edit, and the comment above it now carries the measured
counterfactual rather than an assertion.

This matters more, not less, now that rollback is first-class: a rollback that
landed on the bootstrap image would be an outage created by the tool meant to
end one.

### 7. Rollback is a first-class operation

`action: rollback` with any SHA that is an ancestor of `origin/main` — the
condition is ancestry, never "is the tip of main", so nothing in the workflow
assumes latest.

- Both jobs check out **that SHA**, so the infrastructure applied is that
  commit's, not main's. Rolling the image back while applying today's Terraform
  would be the same class of bug this ADR exists to close.
- The image must already exist in ECR. That is checked in the ungated `plan`
  job, **before anything is mutated**, and the failure names the tag and
  explains the ECR lifecycle policy's 10-image retention — an old SHA's image
  may legitimately be gone, and a half-applied release is the one outcome worse
  than a refused one.
- `assert_models`, `assert_model_env` and `assert_step_models` all run on
  rollbacks, asserting against that SHA's expectations because they execute from
  that SHA's checkout. A rollback that skipped the gates would recreate the
  original bug in the situation you can reason about least.
- The pre-approval summary states that it is a rollback, the target SHA with
  subject and author, and the **currently-running** SHA, so the approver sees
  what production is being moved from as well as to.

## Consequences

- Code and infrastructure cannot drift apart across a release: the same run, off
  one commit, does both. Drift becomes structurally unlikely rather than merely
  detected — `assert_model_env` is now a backstop instead of the only line.
- Every release costs one approval click, including infrastructure-only ones.
  Accepted deliberately (decision 3).
- Releases are slower: a plan and an apply are added to the critical path, and
  the apply happens before the image build.
- The `plan` job needs both roles, so a release now fails early on an OIDC trust
  problem in either. KB-006 makes that a known, checkable failure.
- `terraform.yml` can no longer resync drift on its own. That is the point;
  `Deploy` at the running SHA replaces it, idempotently.
- The from-scratch bootstrap is unchanged and still a local apply on human admin
  credentials — `cadre-terraform` is created by this Terraform (ADR 0001
  decision 7, the part that stands).
- The properties above are asserted in CI against the workflow source itself
  (`.github/tests/test_release_workflow.py`), because nothing else executes this
  path outside a real production release.

## Runbook

**Release.** `gh workflow run deploy.yml -f action=deploy -f sha=<40-char SHA>`,
or the Actions UI. The `plan` job validates the SHA, verifies the image, and
posts the Terraform plan plus a resource-by-resource summary to the run
summary. Read it, then approve the `production` environment. The `release` job
applies that exact plan, runs the model gates, builds and points the Lambda,
ships the page, invalidates CloudFront and smokes `/healthz`, `/` and `/config`.

**Infrastructure-only change.** Exactly the same command with the
currently-running SHA. The image exists, so the build is skipped.

**Rollback.** `gh workflow run deploy.yml -f action=rollback -f sha=<older SHA>`.
The summary names the target and the currently-running SHA before the gate. If
the image has been expired by the 10-image ECR retention policy the run fails in
the `plan` job, before touching production, naming the tag; roll back to a more
recent commit, or use `action=deploy` to rebuild that commit from source.

**Preview infra without shipping.** The `Terraform` workflow — plan only, and it
cannot apply.

## Related

- `.github/workflows/deploy.yml` — 1–7; `.github/workflows/terraform.yml` — 1.
- `.github/scripts/summarize_plan.py` — 3; `.github/tests/test_release_workflow.py` — all.
- `infra/lambda.tf` (`ignore_changes = [image_uri]`, ECR lifecycle policy) — 6, 7.
- `infra/oidc.tf`, `infra/ci_terraform.tf` — 5; `infra/README.md` — the runbook.
- `backend/scripts/assert_model_env.py`, `assert_models.py`, `assert_step_models.py` — 4.
- Issue #84 / PR #88 — the drift this ADR makes structurally hard to repeat.
- ADR 0001 decisions 5, 7, 8; KB-006, KB-009.
