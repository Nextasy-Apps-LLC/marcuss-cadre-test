# CI and deployment

Five workflows in `.github/workflows/`. Only two of them can change
production — the Terraform apply and the deploy — and neither can be triggered
by a merge.

| Workflow | Trigger | Touches AWS? |
|---|---|---|
| `ci.yml` | every PR, pushes to `main`, manual | Only the opt-in `e2e` job, and only Bedrock via an API key — no IAM |
| `terraform.yml` | PRs touching `infra/`, manual | Yes — plan always, apply only on request |
| `deploy.yml` | manual only | Yes |
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

Four jobs run in parallel on every PR and push:

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

=== "image"

    QEMU plus Buildx, then a `linux/arm64` build of `backend/Dockerfile` with
    `push: false` and the GitHub Actions build cache. Catches a broken
    Dockerfile here rather than midway through a deploy.

=== "terraform"

    Terraform 1.13.1. `terraform fmt -check -recursive`, then
    `terraform init -backend=false` and `terraform validate`. The
    `-backend=false` matters: validation needs the providers, not the state,
    so this job never touches the state bucket and needs no AWS credentials.

!!! warning "Nothing between `docker build` and production boots the container"
    The `image` job proves the Dockerfile builds. It never runs the image or
    invokes it, and `update-function-code` succeeds unconditionally — it is
    just a pointer swap. The post-deploy `curl /healthz` is the first thing in
    the entire pipeline that actually starts the container. That gap is how the
    base image's `ENTRYPOINT` swallowing the uvicorn `CMD` reached production
    with CI green. A `docker run -p 8080:8080` smoke step would close it; it is
    not built yet. See
    [ADR 0001, decision 9](adr/0001-streaming-chatbot-cloudfront-lambda-s3.md).

A fifth job, `e2e`, exists but never runs on push or PR: it only fires on a
`workflow_dispatch` with `run_e2e: true`, points the backend e2e suite at a
real target via `e2e_base_url` (default: production), and with
`e2e_live_bedrock` also drives the live-model cases using the `BEDROCK_API_KEY`
repository secret. If that flag is requested and the secret is absent, the job
skips with a visible warning annotation rather than failing or silently
running only the deterministic cases. The gate's reasoning and the local
equivalent are documented in
[`backend/tests/e2e/README.md`](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/backend/tests/e2e/README.md).

## `terraform.yml` — plan on PR, apply on request

Triggers on `pull_request` limited to `paths: ["infra/**", ".github/workflows/terraform.yml"]`,
and on `workflow_dispatch` with an `action` choice of `plan` or `apply`.
Concurrency group `terraform-production`, never cancelled in progress — a
half-applied stack is worse than a queued run.

State configuration is workflow-level `env`, not a secret: the bucket is
`nextasyapps-terraform-state-dev` and the key is `cadre/cadre.tfstate`. A
bucket name grants nothing without the IAM role, and the role is OIDC-gated to
this repository. Backends use `use_lockfile=true` — native S3 state locking,
which needs `required_version >= 1.10` and replaces a DynamoDB lock table.

### The `plan` job

Guarded by `if: vars.TF_ROLE_ARN != ''`. Until the first apply creates the
Terraform role, that variable is unset and the job could only fail with an
opaque AWS auth error; skipping reports honestly as "not configured yet"
instead of red.

It assumes the role via OIDC, inits against the S3 backend, re-runs
`fmt -check` and `validate`, prints `terraform output`, then plans into a
`tfplan` file. Printing outputs here means nobody needs local AWS credentials
just to read a value the stack already exports — the ACM validation record
being the recurring example.

The plan step captures its own exit code with `set +e` so a summary step can
report the result, and the summary uses `|| true` throughout: it reports, it
does not judge. Letting it fail on a missing `plan.txt` would replace the real
error with a `tail(1)` message.

!!! note "Known discrepancy: `-detailed-exitcode` is documented but not passed"
    The comment above the plan step describes `-detailed-exitcode` semantics
    (0 = no changes, 2 = changes, 1 = error), but the `terraform plan`
    invocation does not actually pass the flag. The captured code is therefore
    only ever 0 or 1, and the summary's "Changes pending — review the plan
    below before applying" branch is unreachable. Recorded here rather than
    quietly changed, since a docs PR is the wrong place to alter what CI does.

When dispatched with `action: apply`, the job uploads `infra/tfplan` as an
artifact with a 1-day retention.

### The `apply` job

`needs: plan`, `if: inputs.action == 'apply'`, and runs inside
`environment: production`. It downloads the exact `tfplan` artifact the plan
job produced and runs `terraform apply ... tfplan` — it never re-plans. If
state moved between review and apply, Terraform refuses rather than silently
applying something nobody looked at.

## `deploy.yml` — manual, gated, deploy or rollback

There is no push trigger. Shipping is a decision, and the approval gate would
be meaningless if a merge could fire it. Concurrency group
`deploy-production`, `cancel-in-progress: false` — two concurrent runs would
race on the Lambda pointer and the S3 sync, and the loser would silently win.

Inputs are an `action` (`deploy` or `rollback`) and a full 40-character commit
SHA.

### `plan` — credential-free validation, before the gate

Runs first, deliberately, so a doomed request is rejected without waking a
human. It checks out full history and asserts three things:

1. The SHA matches `^[0-9a-f]{40}$`. Short SHAs are ambiguous, and a deploy is
   the wrong place to find that out.
2. The commit exists (`git cat-file -e`).
3. The commit is an ancestor of `origin/main`
   (`git merge-base --is-ancestor`). Without this you could ship an unreviewed
   branch tip by pasting its SHA, routing around the whole PR and code-owner
   gate.

It then writes a summary table naming the action, commit, subject, author and
requester.

### `deploy` — the gated job

Runs in `environment: production` with `url: ${{ vars.SITE_URL }}`, and
authenticates to AWS by OIDC only — no access key exists for this repository.

1. **Is the image already built?** `aws ecr describe-images` on the SHA tag.
   A `rollback` whose image is missing fails right here: a rollback restores a
   previously deployed build, it never creates one.
2. **Build and push** — skipped entirely on rollback, and skipped on deploy
   when the tag already exists. The ECR repository is `IMMUTABLE`, so a second
   push of the same tag would fail anyway; re-deploying a SHA is idempotent.
3. **Point the Lambda at the image**, then `aws lambda wait function-updated`.
   Without the wait, the smoke test can hit the old code and pass.
4. **Build the page** from source with Node 22 and `npm ci`.
5. **Sync hashed assets first, additively** (no `--delete`) with
   `max-age=31536000, immutable`. A visitor mid-navigation may still be running
   the previous `index.html`; deleting its assets breaks that page in flight.
   Filenames are content-hashed, so stale objects are harmless.
6. **Sync `index.html` last**, `max-age=0, must-revalidate`. It is the pointer
   that makes the new assets live, so uploading it first would reference assets
   that are not there yet.
7. **Invalidate CloudFront** on `/*` and wait for completion.
8. **Smoke test** `/healthz` with five attempts six seconds apart, then `/` —
   proving the page is served, not just the API.
9. **Record the outcome** in the step summary whether or not the run passed.

!!! danger "Never `aws s3 cp --metadata-directive REPLACE` here"
    `aws s3 sync` infers `Content-Type` from the file extension. The `REPLACE`
    directive wipes it along with cache-control, and the browser then refuses
    the stylesheet. Both sync steps use `sync`, split by cache policy with
    `--exclude`/`--include`.

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
`openwiki/` and opens a PR (branch `openwiki/update`) — never a direct push —
so the generated docs go through the same history as everything else. It checks out
with `fetch-depth: 0`: a shallow clone hides the commit OpenWiki last
documented, so the update would diff against an empty change summary.

## Repository settings the workflows depend on

The workflow files describe a gate; they do not create one. `environment: production`
is inert until that environment exists **and** has required reviewers. See
`.github/DEPLOYMENT.md` for the full one-time setup — the approval gate, branch
protection on `main`, and the repository variables (`AWS_REGION`,
`AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY`, `LAMBDA_FUNCTION_NAME`, `WEB_BUCKET`,
`CLOUDFRONT_DISTRIBUTION_ID`, `SITE_URL`, `TF_ROLE_ARN`, `AWS_ACCOUNT_ID`,
`OIDC_PROVIDER_ARN`), all of which come from `terraform output`.

There are exactly two repository **secrets**, neither of them an AWS
credential: `BEDROCK_API_KEY`, read only by the dispatch-gated `e2e` job in
`ci.yml` ([ADR 0002](adr/0002-bedrock-mantle-api-key.md) made the Bedrock key
the stack's one *application* secret), and `OPENCODE_ZEN_API_KEY`, used only
by `openwiki-update.yml`'s generation model. Every value above is an
identifier, not a credential; AWS access is granted by the OIDC trust policy,
not by knowing the string.

!!! warning "The environment-sub trap"
    A job running inside `environment: production` gets
    `sub = repo:<owner>/<repo>:environment:production` in its OIDC token. That
    **replaces** the usual `ref:refs/heads/main` form rather than adding to it,
    so a trust policy listing only the ref form denies the gated `deploy` and
    `terraform apply` jobs while the ungated `plan` job keeps working. This
    org's tokens also carry two spellings of the repo — the name form and the
    id-qualified form that survives renames — so every trust condition is
    (2 spellings) × (applicable sub forms). See
    [ADR 0001, decision 5](adr/0001-streaming-chatbot-cloudfront-lambda-s3.md).
