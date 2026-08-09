# Deploying cadre

This page is the **one-time repository setup** for the gate that shipping sits
behind. (The repo has six workflows in total — `terraform.yml` (plan-only since
ADR 0003), `docs.yml`, `openwiki-update.yml` and `diff-honesty-scanner.yml` are
covered in [docs/ci-cd.md](../docs/ci-cd.md). Only `deploy.yml`'s `release` job
uses the `production` environment configured below.)

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | every push and PR | web typecheck + unit tests + build, backend tests, arm64 image build, `terraform fmt`/`validate` |
| `deploy.yml` | manual only | deploy or roll back to a chosen commit, behind a code-owner approval |

`deploy.yml` has no push trigger on purpose. Shipping is a decision, and the
approval gate would be meaningless if a merge could fire it.

---

## ⚠️ One-time setup — the gate does not exist until you do this

`environment: production` in the workflow file **is inert on its own**. Until
the environment exists *and* has required reviewers, `deploy.yml` runs
immediately with no approval. This is the single most important thing on this
page.

### 1. Default branch

Settings → General → Default branch → **`main`**.

(The repo was created empty, so the first branch pushed became the default.
This is cosmetic but confusing, and some rules below key off the default.)

### 2. The approval gate

Settings → Environments → **New environment** → name it `production`.

- ✅ **Required reviewers** → add `@marcuss`
- Optionally set a **wait timer** for a change-of-mind window
- Deployment branches → **Selected branches** → `main`

Without required reviewers here, nothing gates the deploy.

### 3. Branch protection on `main`

Settings → Rules → Rulesets → **New branch ruleset**, targeting `main`:

- ✅ Require a pull request before merging
  - Required approvals: **1**
  - ✅ Require review from Code Owners
  - ✅ Dismiss stale approvals when new commits are pushed
- ✅ Require status checks to pass
  - `web — typecheck, test, build`
  - `backend — tests`
  - `backend — image builds`
  - `terraform — fmt and validate`
- ✅ Block force pushes

> **The single-owner catch.** GitHub does not let a PR author approve their own
> PR. `.github/CODEOWNERS` lists one owner (`@marcuss`), so a PR *you* open
> cannot satisfy "Require review from Code Owners" on its own. Either:
>
> - leave **admin bypass** available on the ruleset (pragmatic for a solo repo), or
> - add a second code owner, or
> - drop "Require review from Code Owners" and keep plain required approvals.
>
> An org-wide team was considered and rejected: a team containing everyone
> means anyone can approve anything, which is a formality rather than a gate.

### 4. Repository variables

Settings → Secrets and variables → Actions → **Variables**. All of these come
from `terraform output`. None are secrets — the role ARN is an identifier, and
access is granted by the OIDC trust policy, not by knowing the string.

| Variable | Source |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `AWS_DEPLOY_ROLE_ARN` | `terraform output ci_role_arn` |
| `ECR_REPOSITORY` | `cadre` (the `project_name`) |
| `LAMBDA_FUNCTION_NAME` | `terraform output lambda_function_name` |
| `WEB_BUCKET` | `terraform output web_bucket` |
| `CLOUDFRONT_DISTRIBUTION_ID` | `terraform output cloudfront_distribution_id` |
| `SITE_URL` | `terraform output site_url` |
| `TF_ROLE_ARN` | `terraform output terraform_role_arn` (gates `terraform.yml`'s plan job — unset means "skip cleanly, not configured yet") |
| `AWS_ACCOUNT_ID` | the 12-digit account id (`terraform.yml` passes it as `-var`) |
| `OIDC_PROVIDER_ARN` | the shared GitHub OIDC provider ARN (looked up, never created — ADR 0001) |

There are exactly two repository **secrets**, neither an AWS credential:
`BEDROCK_API_KEY` (only the dispatch-gated `e2e` job in `ci.yml`; ADR 0002)
and `OPENCODE_ZEN_API_KEY` (only `openwiki-update.yml`'s generation model).
Before adding another, check whether OIDC or an SSM parameter fits — see
`infra/README.md`.

---

## Deploying

Actions → **Deploy** → Run workflow:

- **Action**: `deploy`
- **SHA**: the full 40-character commit SHA

The ungated `plan` job validates the SHA and posts that commit's Terraform plan
to the run summary; the run then pauses at the `production` environment until a
reviewer approves. Then the `release` job:

1. Applies the exact Terraform plan the summary showed (never a re-plan — ADR 0003)
2. Runs the model gates (`assert_models`, `assert_model_env`)
3. Builds the arm64 image and pushes it to ECR tagged with the SHA
4. Points the Lambda at that image and waits for the update to settle
5. Builds the page, syncs hashed assets, then `index.html`
6. Invalidates CloudFront and waits for it to complete
7. Smoke-tests `/healthz`, `/` and `/config` (`assert_step_models`)

Re-deploying a SHA that was already built is idempotent — the ECR repository is
`IMMUTABLE`, so the build is skipped and the existing image reused.

## Rolling back

Same form, **Action: `rollback`**, SHA of the last known-good commit.

The difference is not cosmetic: rollback **refuses to build**. If no image is
tagged with that SHA, the run fails and tells you so. A rollback restores
something that was previously running; if it can build a new artifact, it is a
deploy wearing a rollback label, and the thing you get back is not the thing
you had. The page is rebuilt from source either way — it is a deterministic
static build and takes about a second.

**Rollback is not instant.** It waits on a CloudFront invalidation, which is
typically under a minute but is not guaranteed. If you need a faster lever,
that is a different design (a versioned alias shift), not a faster version of
this one.

## What can be deployed

Only commits that are ancestors of `origin/main`. `plan` enforces this before
the approval gate, so pasting a branch tip cannot route around the PR and
code-owner review. Short SHAs are rejected — they are ambiguous, and a deploy
is the wrong place to find that out.
