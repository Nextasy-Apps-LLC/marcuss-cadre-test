# CI and deployment

Six workflows in `.github/workflows/`. Exactly one of them can change
production — **`Deploy`** — and it fires only on manual dispatch, behind an
approval gate. [ADR 0003](adr/0003-one-gated-release-path.md) is the why:
code, page and infrastructure ship in one gated run so they cannot drift
apart across a release.

| Workflow | Trigger | Touches AWS? |
|---|---|---|
| `ci.yml` | every PR, pushes to `main`, manual | Only the opt-in `e2e` jobs, and only Bedrock via an API key — no IAM |
| `deploy.yml` | manual dispatch only, gated | Yes — plans *and applies* Terraform, then ships the release |
| `terraform.yml` | PRs touching `infra/`, manual | Plan only — it has no apply job and no path to one |
| `diff-honesty-scanner.yml` | every PR | No — reads the diff and the PR body |
| `docs.yml` | pushes to `main` touching docs, manual | No — publishes to GitHub Pages |
| `openwiki-update.yml` | daily schedule, manual | No — regenerates `openwiki/` via a PR |

## `ci.yml` — the gate on every change

Runs on `pull_request` (any branch) and on `push` to `main` only. The
main-only push trigger is deliberate: `branches: ["**"]` would also match the
source branch of an open PR, so every push to a PR branch fired two identical
runs — double the minutes and a duplicated checks list. Branch coverage comes
from the `pull_request` trigger instead.

Concurrency is grouped per ref with `cancel-in-progress: true`; a newer push
makes the in-flight run irrelevant. Permissions are `contents: read` — CI never
assumes an AWS IAM role.

Six jobs run on every PR and push:

=== "web"

    Node 22 with the npm cache keyed on `web/package-lock.json`. Installs with
    `npm ci` rather than `npm install`, so a lockfile that disagrees with
    `package.json` fails here instead of silently resolving something new.
    Then `npm run typecheck`, `npm test` (Vitest), `npm run build`.

    The built `web/dist` is uploaded as an artifact with a 7-day retention.
    Nothing consumes it — the deploy workflow rebuilds from source so a
    deployment stays reproducible from a SHA alone. It exists for inspection
    when a build looks wrong.

=== "backend"

    Python 3.13 with the pip cache keyed on `backend/requirements-dev.txt`,
    then `pytest -q`.

=== "release-path"

    Runs `pytest .github/tests/ -q`: tests that pin the *workflows themselves*
    — the approval gate stays unconditional, `terraform apply` keeps consuming
    the reviewed plan file, the honesty scanner's self-test stays first. The
    `Deploy` workflow runs a couple of times a week in production with a human
    waiting; it is the worst place to discover the gate drifted.

=== "image"

    QEMU plus Buildx, then a `linux/arm64` build of `backend/Dockerfile` with
    `push: false` and the GitHub Actions build cache. Catches a broken
    Dockerfile here rather than midway through a deploy.

=== "terraform"

    Terraform 1.13.1. `terraform fmt -check -recursive`, then
    `terraform init -backend=false` and `terraform validate`. The
    `-backend=false` matters: validation needs the providers, not the state,
    so this job never touches the state bucket and needs no AWS credentials.

=== "e2e-web"

    Builds the backend image in-job, starts the container, and drives the
    model-free Playwright tier against it in a real browser (#97). This is the
    job that closed the gap behind the base image's `ENTRYPOINT` swallowing
    the uvicorn `CMD` ([ADR 0001, decision 9](adr/0001-streaming-chatbot-cloudfront-lambda-s3.md)):
    until it existed, nothing between `docker build` and production ever
    booted the container, so that defect shipped with CI green.

Two more jobs fire only on a `workflow_dispatch` with `run_e2e: true`, pointed
at a real target via `e2e_base_url` (default: production): `e2e` runs the
backend's `BASE_URL`-pointable suite, and `e2e-web-live` runs the
model-dependent `@live` browser specs. With `e2e_live_bedrock` also set they
drive the live-model cases using the `BEDROCK_API_KEY` repository secret; if
that flag is requested and the secret is absent, the job skips with a visible
warning annotation rather than failing or silently running only the
deterministic cases. The gate's reasoning and the local equivalent are in
[`backend/tests/e2e/README.md`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/backend/tests/e2e/README.md)
and [`web/e2e/README.md`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/web/e2e/README.md).

## `deploy.yml` — the one release path

There is no push trigger. Shipping is a decision, and the approval gate would
be meaningless if a merge could fire it. Concurrency group
`deploy-production`, `cancel-in-progress: false` — two concurrent runs would
race on the Lambda pointer and the S3 sync, and the loser would silently win.

Inputs are an `action` (`deploy` or `rollback`) and a full 40-character commit
SHA. Two jobs:

**`plan` — everything checkable, before the gate.** Validates the SHA (shape,
existence, and ancestry of `origin/main` — without the ancestor check you
could ship an unreviewed branch tip by pasting its SHA, routing around the
whole PR gate), then assumes the Terraform role and runs `terraform plan`
into a saved `tfplan` artifact, with a human-readable summary for the
approver. A doomed request is rejected without waking a human.

**`release` — the gated job.** Runs in `environment: production` (required
reviewers configured in repo settings), and then, in order:

1. **Apply the reviewed plan** — the exact `tfplan` artifact, never a re-plan,
   so infrastructure and code cannot drift apart between approval and apply.
   This runs *first* because `assert_model_env` refuses to deploy against an
   environment only an apply can fix — the drift incident behind ADR 0003.
2. **`assert_models`** — every configured Bedrock model id is invocable in
   this account (#84).
3. **`assert_model_env`** — the live Lambda environment already matches the
   models this commit ships, so a deploy never swaps the image and leaves the
   roster behind.
4. **Build and push the image** (skipped on rollback and when the immutable
   ECR tag already exists), **point the Lambda at it**, and
   `wait function-updated` — without the wait the smoke test can hit the old
   code and pass.
5. **Ship the page**: sync hashed assets first (additively, `immutable`),
   `index.html` last (`max-age=0`) — it is the pointer that makes the new
   assets live. Never `aws s3 cp --metadata-directive REPLACE`: `sync` infers
   `Content-Type` from the extension and `REPLACE` wipes it, and the browser
   then refuses the stylesheet.
6. **Invalidate CloudFront**, wait, then **smoke** `/healthz` (five attempts,
   six seconds apart) and `/` — proving the page is served, not just the API.
7. **`assert_step_models`** — `/healthz` proves something is up; this proves
   the live service reports the models *this commit* deployed.

The operator runbook — including rollback — lives in
[infra/README.md §Releasing](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/infra/README.md#releasing);
the incident that forced one path and the two plausible-sounding fixes that
were rejected are in [ADR 0003](adr/0003-one-gated-release-path.md).

## `terraform.yml` — plan only

Triggers on PRs touching `infra/**` and on manual dispatch. It assumes the
Terraform role via OIDC, inits against the S3 backend, re-runs `fmt -check`
and `validate`, prints `terraform output` (so nobody needs local AWS
credentials just to read a value the stack already exports), and plans with
`-detailed-exitcode` so the summary can distinguish "changes pending" from
"clean". It cannot apply: no apply job exists, the job never enters the
`production` environment, and ADR 0003 is why that is a property, not a gap —
before #93 this workflow *could* apply, which is exactly how infrastructure
and code drifted.

State configuration is workflow-level `env`, not a secret: the bucket is
`nextasyapps-terraform-state-dev` and the key is `cadre/cadre.tfstate`. A
bucket name grants nothing without the IAM role, and the role is OIDC-gated to
this repository. Backends use `use_lockfile=true` — native S3 state locking,
which needs `required_version >= 1.10` and replaces a DynamoDB lock table.
Concurrency group `terraform-production`, never cancelled in progress — a
half-read state is worse than a queued run.

## `diff-honesty-scanner.yml` — the safety net guards itself

Runs on every PR. The scanner (`.github/scripts/diff_honesty_scanner.py`,
stdlib-only, twelve rule families) fails a PR whose diff *weakens the safety
net* — deleting or softening tests, skipping gates, widening fail-open paths —
unless the PR body carries an explicit
`honesty-waiver: <rule> <exact/path> — <reason>` line per waived finding, so a
waiver is itself reviewable text. A diff that modifies the scanner machinery
is never waivable.

The job's first step runs the scanner's own fixture suite
(`.github/tests/test_diff_honesty_scanner.py`): a scanner that cannot catch
its fixture violations fails red before it judges anyone. Issue #86 has the
full rationale.

## `docs.yml` — this site

Builds `mkdocs.yml` with `--strict` and publishes to GitHub Pages via
`actions/upload-pages-artifact` and `actions/deploy-pages`. Triggers on pushes
to `main` that touch `docs/**`, `mkdocs.yml`, `adr/**` or the workflow itself,
plus manual dispatch. `adr/**` is in the path filter because the ADR pages here
embed those files directly — a new ADR that did not rebuild the site would be
invisible.

It uses the GitHub OIDC-based Pages deployment (`id-token: write`,
`environment: github-pages`) rather than pushing to a `gh-pages` branch, so
there is no build output in git history and no branch-write permission handed
to a workflow.

To build it locally:

```bash
pip install -r docs/requirements.txt
mkdocs serve            # live preview on :8000
mkdocs build --strict   # exactly what CI runs
```

## `openwiki-update.yml` — the repo knowledge base

Runs on a daily schedule plus manual dispatch. It regenerates the pages under
`openwiki/` and refreshes the `OPENWIKI` marker blocks in `CLAUDE.md` /
`AGENTS.md`, then opens a PR (branch `openwiki/update`) — never a direct
push — so the generated docs go through the same history as everything else.
It checks out with `fetch-depth: 0`: a shallow clone hides the commit OpenWiki
last documented, so the update would diff against an empty change summary.

## Repository settings the workflows depend on

The workflow files describe a gate; they do not create one. `environment: production`
is inert until that environment exists **and** has required reviewers. See
`.github/DEPLOYMENT.md` for the full one-time setup — the approval gate, branch
protection on `main`, and the repository variables (`AWS_REGION`,
`AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY`, `LAMBDA_FUNCTION_NAME`, `WEB_BUCKET`,
`CLOUDFRONT_DISTRIBUTION_ID`, `SITE_URL`, `TF_ROLE_ARN`, `AWS_ACCOUNT_ID`,
`OIDC_PROVIDER_ARN`), all of which come from `terraform output`.

There are exactly two repository **secrets**, neither of them an AWS
credential: `BEDROCK_API_KEY`, read only by the dispatch-gated `e2e` jobs in
`ci.yml` ([ADR 0002](adr/0002-bedrock-mantle-api-key.md) made the Bedrock key
the stack's one *application* secret), and `OPENCODE_ZEN_API_KEY`, used only
by `openwiki-update.yml`'s generation model. Every value above is an
identifier, not a credential; AWS access is granted by the OIDC trust policy,
not by knowing the string.

!!! warning "The environment-sub trap"
    A job running inside `environment: production` gets
    `sub = repo:<owner>/<repo>:environment:production` in its OIDC token. That
    **replaces** the usual `ref:refs/heads/main` form rather than adding to it,
    so a trust policy listing only the ref form denies the gated `release`
    job while the ungated `plan` jobs keep working. This
    org's tokens also carry two spellings of the repo — the name form and the
    id-qualified form that survives renames — so every trust condition is
    (2 spellings) × (applicable sub forms). See
    [ADR 0001, decision 5](adr/0001-streaming-chatbot-cloudfront-lambda-s3.md).
