# infra/CLAUDE.md — Terraform guidelines

All Terraform lives in `infra/`, one flat root module. Sources: HashiCorp
style guide, Google's Terraform best practices, Gruntwork — filtered to what
this stack does. Rules, each with its why:

## Hygiene

- `terraform fmt -recursive` and `terraform validate` locally before pushing — CI hard-fails both.
- `.terraform.lock.hcl` is committed. After a provider bump, regenerate for both platforms that run init: `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`. Single-platform lockfiles fail everywhere but the machine that wrote them.
- One file per concern (`lambda.tf`, `cloudfront.tf`, `oidc.tf`, …), no `main.tf` catch-all. `versions.tf` owns versions, backend, provider.
- snake_case labels, no type echo in names (`aws_s3_bucket.web`); singleton resources are named `this`.
- Comment the *why* on any setting that looks optional but breaks something when flipped — the four streaming-breakers in `cloudfront.tf` are the canon. Never touch them without re-reading ADR 0001.

## Variables and outputs

- Every variable: `type` + `description` (the description says why the value is what it is). Add `validation` where a wrong value is expensive.
- Units in numeric names (`lambda_timeout_s`); booleans positive (`enable_custom_domain` — a two-phase bootstrap flag: keep it, and never add `aws_acm_certificate_validation`; DNS is in Cloudflare, validation is a human step).
- No secret defaults; no account ids in defaults — this repo is public. Gitignored `terraform.tfvars` / `backend.hcl` with committed `.example` files.
- First real secret = SSM `SecureString`, `value = "SET_OUT_OF_BAND"`, `ignore_changes = [value]`, real value via `aws ssm put-parameter`. Never tfvars, never an output.
- Outputs are the no-credentials interface: describe what to do with each; the CI plan job prints them so nobody needs local creds.

## State

- Partial backend config only (`backend "s3" {}` + `backend.hcl` / CI flags). Native S3 locking (`use_lockfile`, TF ≥ 1.10) — no DynamoDB table.
- Applies happen in CI from the reviewed `tfplan` artifact, never a re-plan in the apply job. Only sanctioned local apply: the from-scratch bootstrap (the CI role is created by this Terraform). A stale bootstrap-era local `terraform.tfvars` is a live footgun, not a hypothetical one — see issue #37, where a local apply with a forgotten `enable_custom_domain = false` silently reverted a working `cadre.marcuss.pro` back to the default cert. `aws_cloudfront_distribution.this` carries a `lifecycle.precondition` guarding exactly that case, but the rule stands generally: once bootstrap is done, resync drift via the CI `Deploy` workflow at the currently-running SHA, never a local apply. Since ADR 0003 the `Terraform` workflow is plan-only and cannot apply at all — `Deploy` plans before its approval gate and applies that exact plan after it, so a commit's code and its infrastructure ship together.
- Never hand-edit state. Un-manage without destroying via a `removed {}` block in a reviewed PR — never a `null_resource` provisioner running `terraform state rm` (it fires mid-apply, after the destroy is already planned).
- Drift under `ignore_changes` is silent, not absent: resync with `terraform apply -refresh-only`, never by dropping the ignore.

## IAM

- `cadre-deploy` is resource-scoped and ships code only. It must never gain `lambda:UpdateFunctionConfiguration` — env vars belong to Terraform; that grant would let a compromised deploy repoint the origin. Wrong by definition (ADR 0001). **Model ids are the exception to "env vars belong to Terraform", and in the same direction:** since issue #84 they are not env vars at all. They live in `backend/app/config.py`'s `MODEL_DEFAULTS`, baked into the reviewed image beside the prompts they were benchmarked against, because a Terraform variable silently beat the code default and production ran the wrong roster for weeks. Nothing in the deploy path can repoint a model now either. Do not re-add a `*_model` variable here: `backend/scripts/assert_model_env.py` reads the function's live environment before every build and fails the deploy on any `CADRE_MODEL_*` it finds, whoever set it.
- `cadre-terraform` is service-scoped — the honest limit for a role that creates not-yet-existing ARNs. The real control is the `production` environment gate on the apply job: never remove it. IAM writes stay scoped to `role/${project_name}-*`.
- OIDC trust = (2 repo spellings: name + id-qualified) × (sub forms), and `environment: production` *replaces* the ref sub. Miss one and CI dies with a bare `sts:AssumeRoleWithWebIdentity` denial — only after the approval click.
- Never create or import the GitHub OIDC provider — account singleton, referenced by ARN.
- Widen permissions from the CloudWatch AccessDenied error (it names action + resource), never by guessing.

## Workflows and lifecycle

- No one-shot migration commands in workflow YAML — it re-runs forever. One-time ops: declarative in `.tf`, a documented manual step in the PR body, or a temporary workflow deleted after dispatch.
- Every `ignore_changes` carries a why-comment and exactly one out-of-band owner (`image_uri` → deploy.yml; SSM values → `put-parameter`). Two writers race.

## Debugging 403s

- Generic Lambda-URL 403? Bisect with an in-account SigV4 call (signed curl / `awscurl`) **before** re-reading the OAC config. Signed call works → Lambda side is proven; the gap is the caller's grant.
- The two-permission trap (found here, 2026-08-07): Function URLs created since Oct 2025 need `lambda:InvokeFunction` **and** `lambda:InvokeFunctionUrl`, and the missing-grant 403 body is identical to a bad-signature 403. Check both statements first.
- Viewer POSTs must send `x-amz-content-sha256` (OAC signs over it); GETs are exempt. And a curl-green smoke ≠ streaming works — curl ignores alt-svc; verify streaming in a browser.
